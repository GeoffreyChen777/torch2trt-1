import torch
import torchvision
import torch2trt
import time
import numpy as np
from tvm.contrib import graph_runtime

import tvm
from tvm.relay import expr, analysis
from tvm import relay


def benchmark_trt_torch(net, input_shape=(1, 3, 224, 224)):
    net = net.float().cuda()
    param_exclude = ".*AuxLogits.*"  # for inception v3
    net_trt = torch2trt.TensorRTModuleWrapper(
        net,
        input_shape[0],
        1 << 30,
        param_exclude=param_exclude,
        verbose=True).eval()
    net_trt = net_trt.float().cuda()
    inputs = torch.rand(*input_shape).float().cuda()
    out = net_trt(inputs)  # build trt engine
    times = []
    for i in range(100):
        torch.cuda.synchronize()
        t = time.time()
        out = net_trt(inputs)
        torch.cuda.synchronize()
        times.append(time.time() - t)

    print("tensorrt time:", np.mean(times[2:]))
    out = net(inputs)
    times = []
    for i in range(100):
        torch.cuda.synchronize()
        t = time.time()
        out = net(inputs)
        torch.cuda.synchronize()
        times.append(time.time() - t)

    print("pytorch time:", np.mean(times[2:]))


class TVMInferenceContext:
    def __init__(self, graph, lib, params, ctx, input_names):
        self.graph = graph
        self.lib = lib
        self.ctx = ctx

        self.tvm_context = graph_runtime.create(graph, lib, ctx)
        self.params = params
        self.tvm_context.set_input(**params)
        self.input_names = input_names

    def execute_async(self):
        self.tvm_context.run()

    def inference_async(self, *args):
        assert len(args) == len(self.input_names)
        for k, v in zip(self.input_names, args):
            self.tvm_context.set_input(k, tvm.nd.array(v))
        self.tvm_context.run()
        num_outputs = self.tvm_context.get_num_outputs()
        outputs = []
        for i in range(num_outputs):
            outputs.append(self.tvm_context.get_output(i).asnumpy())
        if len(outputs) == 1:
            return outputs[0]
        return tuple(outputs)


def benchmark_tvm(net, input_shape=(1, 3, 224, 224)):
    param_exclude = ".*AuxLogits.*"  # for inception v3
    inputs = torch.rand(*input_shape).float()
    with torch2trt.core.tvm_network():
        trace, graph_pth = torch2trt.core.torch2tvm(
            net,
            inputs,
            input_names=["image"],
            verbose=True,
            param_exclude=param_exclude)

    outputs = graph_pth.get_resolved_outputs()
    tvm_weight_dict = graph_pth.context.tvm_weight_dict
    params = {k.name_hint: v for k, v in tvm_weight_dict.items()}
    func = expr.Function(analysis.free_vars(outputs), outputs)
    target = 'cuda -libs=cudnn'
    with relay.build_config(opt_level=3):
        graph, lib, params = relay.build(func, target, params=params)
    ctx = TVMInferenceContext(graph, lib, params, tvm.gpu(0), ["image"])
    times = []
    ctx.inference_async(inputs.numpy())
    for i in range(100):
        torch.cuda.synchronize()
        t = time.time()
        ctx.execute_async() # use this to avoid htod and dtoh overhead.
        torch.cuda.synchronize()
        times.append(time.time() - t)

    print("tvm time:", np.mean(times[2:]))

if __name__ == "__main__":
    # net = torchvision.models.resnet50(pretrained=True).eval()
    # net = torchvision.models.vgg19_bn(pretrained=True).eval()
    net = torchvision.models.inception_v3(pretrained=True).eval()
    # net = torchvision.models.squeezenet1_1(pretrained=True).eval()
    # benchmark_trt_torch(net, [1, 3, 224, 224])
    benchmark_tvm(net, [1, 3, 299, 299])
