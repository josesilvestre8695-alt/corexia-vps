import numpy as np, onnx
from onnx import helper, TensorProto
# modelo ONNX minimo (Relu)
X = helper.make_tensor_value_info('X', TensorProto.FLOAT, [1, 3, 64, 64])
Y = helper.make_tensor_value_info('Y', TensorProto.FLOAT, [1, 3, 64, 64])
node = helper.make_node('Relu', ['X'], ['Y'])
g = helper.make_graph([node], 'g', [X], [Y])
m = helper.make_model(g, opset_imports=[helper.make_opsetid("", 13)])
m.ir_version = 10   # ort 1.20 suporta ate IR 10
onnx.save(m, '/tmp/tiny.onnx')

import onnxruntime as ort
print("versao ort:", ort.__version__)
try:
    sess = ort.InferenceSession('/tmp/tiny.onnx', providers=['CUDAExecutionProvider'])
    used = sess.get_providers()
    out = sess.run(None, {'X': np.random.rand(1, 3, 64, 64).astype('float32')})
    print("SESSION providers:", used)
    print("RODOU NA GPU:", 'CUDAExecutionProvider' in used, "| saida:", out[0].shape)
except Exception as e:
    print("FALHOU CUDA:", str(e)[:400])
