"""Execute upstream RMVPE in eager mode for the bundled Torch 2.0 runtime.

CUDA graph capture is an optional optimization, not an inference algorithm.
Keeping it disabled also preserves the existing bounded OOM recovery path.
"""


def run_cuda_graph(owner, key, operation, *inputs):
    return operation(*inputs)
