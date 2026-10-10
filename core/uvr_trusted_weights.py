"""Authenticate shipped UVR checkpoint bytes before any legacy torch loader."""
import hashlib
import io
import os
import types

APPROVED = {
    'HP2_all_vocals.pth': (63454827,'39796caa5db18d7f9382d8ac997ac967bfd85f7761014bb807d2543cc844ef05'),
    'VR-DeEchoNormal.pth': (63666335,'56aba59db3bcdd14a14464e62f3129698ecdea62eee0f003b9360923eb3ac79e'),
    'VR-DeEchoAggressive.pth': (63666335,'8c8fd1582f9aabc363e47af62ddb88df6cae7e064cae75bbf041a067a5e0aee2'),
    'VR-DeEchoDeReverb.pth': (111925279,'01376dd2a571bf3cb9cced680732726d2d732609d09216a610b0d110f133febe'),
}


def authenticated_uvr_bytes(path):
    name=os.path.basename(os.fspath(path))
    if name not in APPROVED:raise ValueError('UVR 仅接受已审计的托管 checkpoint，不能加载任意 pickle')
    size,digest=APPROVED[name]
    with open(path,'rb') as handle:data=handle.read(size+1)
    if len(data)!=size or hashlib.sha256(data).hexdigest()!=digest:
        raise ValueError('UVR 托管权重 SHA-256 / 大小校验失败：'+name)
    return data,{'loader':'authenticated_immutable_checkpoint_stream','name':name,'size':size,'sha256':digest}


def create_trusted_uvr_model(model_class,model_path,**parameters):
    data,receipt=authenticated_uvr_bytes(model_path)
    constructor=model_class.__init__
    if not isinstance(constructor,types.FunctionType):raise ValueError('UVR constructor must be trusted Python code')
    torch=constructor.__globals__['torch']
    expected=os.path.normcase(os.path.abspath(model_path))
    class TorchProxy:
        def __getattr__(self,name):return getattr(torch,name)
        def load(self,path,*args,**kwargs):
            if os.path.normcase(os.path.abspath(os.fspath(path)))!=expected:
                raise ValueError('UVR constructor attempted a different checkpoint')
            # The vendor keeps the filename string for architecture selection.
            # Only its load call uses immutable authenticated bytes, so an
            # in-place replacement between verification and load is harmless.
            return torch.load(io.BytesIO(data),*args,**kwargs)
    namespace=dict(constructor.__globals__,torch=TorchProxy())
    cloned=types.FunctionType(constructor.__code__,namespace,constructor.__name__,constructor.__defaults__,constructor.__closure__)
    cloned.__kwdefaults__=constructor.__kwdefaults__
    approved_class=type('Authenticated'+model_class.__name__,(model_class,),{'__init__':cloned})
    instance=approved_class(model_path=model_path,**parameters)
    instance.authenticated_checkpoint=receipt
    return instance
