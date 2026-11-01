"""
Build models/dfn3_stream.onnx from the official DeepFilterNet3 ONNX export
(models/DeepFilterNet3_onnx.tar.gz in github.com/Rikorose/DeepFilterNet).

The official export is three sequence models (enc, erb_dec, df_dec) that libDF's
tract runtime turns into streaming ones. ONNX Runtime can't do that, so this merges
them into one per-frame graph: the causal time Pads are removed (the caller feeds the
last 3 feature frames), GRU states become inputs/outputs, and the DF decoder keeps its
4 frames of c0 history as a state too. Checked against the originals at the end.

    uv run --no-project --with onnx --with onnxruntime --with numpy \
        python tools/make_dfn_stream.py DeepFilterNet3_onnx.tar.gz models/dfn3_stream.onnx
"""
import sys
import tarfile

import numpy as np
import onnx
import onnxruntime as ort
from onnx import TensorProto, helper, numpy_helper
from onnx.compose import add_prefix

H = 256


def vi(name, shape):
    return helper.make_tensor_value_info(name, TensorProto.FLOAT, shape)


def load_export(path):
    with tarfile.open(path) as tar:
        files = {m.name.rsplit("/", 1)[-1]: m for m in tar.getmembers() if m.isfile()}
        return {n: tar.extractfile(files[f"{n}.onnx"]).read() for n in ("enc", "erb_dec", "df_dec")}


def strip_pads(g):
    alias = {n.output[0]: n.input[0] for n in g.node if n.op_type == "Pad"}
    keep = [n for n in g.node if n.op_type != "Pad"]
    for n in keep:
        n.input[:] = [alias.get(x, x) for x in n.input]
    del g.node[:]
    g.node.extend(keep)


def expose_grus(g, tag):
    for k, n in enumerate([n for n in g.node if n.op_type == "GRU"]):
        n.input[5] = f"h_{tag}{k}"
        g.input.append(vi(n.input[5], [1, 1, H]))
        g.node.append(helper.make_node("Identity", [n.output[1]], [f"h_{tag}{k}_out"]))
        g.output.append(vi(f"h_{tag}{k}_out", [1, 1, H]))


def add_c0_history(g):
    for n in g.node:
        n.input[:] = ["c0_win" if x == "c0" else x for x in n.input]
    g.input.append(vi("c0_hist", [1, 64, 4, 96]))
    g.initializer.extend(numpy_helper.from_array(np.array([v], np.int64), k)
                         for k, v in (("sl_start", 1), ("sl_end", 5), ("sl_axis", 2)))
    g.node.extend([helper.make_node("Concat", ["c0_hist", "c0"], ["c0_win"], axis=2),
                   helper.make_node("Slice", ["c0_win", "sl_start", "sl_end", "sl_axis"],
                                    ["c0_hist_out"])])
    g.output.append(vi("c0_hist_out", [1, 64, 4, 96]))


def prune(nodes, initializers, outputs):
    need = {o.name for o in outputs}
    keep = []
    for n in reversed(nodes):
        if any(o in need for o in n.output):
            keep.append(n)
            need.update(x for x in n.input if x)
    return keep[::-1], [t for t in initializers if t.name in need]


def build(raw):
    enc, erb, dfd = (onnx.load_from_string(raw[n]) for n in ("enc", "erb_dec", "df_dec"))
    for m, tag in ((enc, "enc"), (erb, "erb"), (dfd, "df")):
        strip_pads(m.graph)
        expose_grus(m.graph, tag)
    add_c0_history(dfd.graph)
    enc, erb, dfd = add_prefix(enc, "enc/"), add_prefix(erb, "erb/"), add_prefix(dfd, "df/")

    link = {f"erb/{k}": f"enc/{k}" for k in ("emb", "e0", "e1", "e2", "e3")}
    link.update({"df/emb": "enc/emb", "df/c0": "enc/c0"})
    for m in (erb, dfd):
        for n in m.graph.node:
            n.input[:] = [link.get(x, x) for x in n.input]

    parts = (enc, erb, dfd)
    inputs = [i for m in parts for i in m.graph.input if i.name not in link]
    outputs = [vi(n, None) for n in ("enc/lsnr", "erb/m", "df/coefs")] + \
              [o for m in parts for o in m.graph.output if o.name.endswith("_out")]
    nodes, inits = prune([n for m in parts for n in m.graph.node],
                         [t for m in parts for t in m.graph.initializer], outputs)
    model = helper.make_model(helper.make_graph(nodes, "dfn3_stream", inputs, outputs, inits),
                              opset_imports=enc.opset_import)
    model.ir_version = enc.ir_version
    return model.SerializeToString()


def verify(raw, stream):
    rng = np.random.default_rng(0)
    T = 60
    fe = rng.standard_normal((1, 1, T, 32)).astype(np.float32) * 0.5
    fs = rng.standard_normal((1, 2, T, 96)).astype(np.float32) * 0.5
    so = ort.SessionOptions()
    so.log_severity_level = 3
    sess = lambda b: ort.InferenceSession(b, so, providers=["CPUExecutionProvider"])
    e0, e1, e2, e3, emb, c0, lsnr = sess(raw["enc"]).run(None, {"feat_erb": fe, "feat_spec": fs})
    m_ref = sess(raw["erb_dec"]).run(None, {"emb": emb, "e3": e3, "e2": e2, "e1": e1, "e0": e0})[0]
    c_ref = sess(raw["df_dec"]).run(None, {"emb": emb, "c0": c0})[0]

    st = sess(stream)
    state = {i.name: np.zeros(i.shape, np.float32) for i in st.get_inputs()
             if not i.name.startswith("enc/feat")}
    fe = np.concatenate([np.zeros((1, 1, 2, 32), np.float32), fe], 2)
    fs = np.concatenate([np.zeros((1, 2, 2, 96), np.float32), fs], 2)
    names = [o.name for o in st.get_outputs()]
    err = 0.0
    for t in range(T):
        out = dict(zip(names, st.run(None, {"enc/feat_erb": fe[:, :, t:t + 3],
                                            "enc/feat_spec": fs[:, :, t:t + 3], **state})))
        state = {k: out[k + "_out"] for k in state}
        err = max(err, np.abs(out["erb/m"].ravel() - m_ref[0, 0, t]).max(),
                  np.abs(out["df/coefs"].ravel() - c_ref[0, t].ravel()).max(),
                  abs(out["enc/lsnr"].item() - lsnr.ravel()[t]) / 100)
    print(f"max deviation from the original models: {err:.2e}")
    assert err < 1e-4


if __name__ == "__main__":
    raw = load_export(sys.argv[1])
    stream = build(raw)
    verify(raw, stream)
    with open(sys.argv[2], "wb") as f:
        f.write(stream)
    print("wrote", sys.argv[2], f"{len(stream) / 1e6:.1f} MB")
