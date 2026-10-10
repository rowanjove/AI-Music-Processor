"""Worker-local fix for trusted UVR's uninitialized spectral band buffers."""
import types


def install_zero_band_buffers(instance):
    original = instance._path_audio_.__func__
    spec_utils = original.__globals__['spec_utils']
    combine = spec_utils.cmb_spectrogram_to_wave
    numpy = combine.__globals__['np']

    class NumpyProxy:
        def __getattr__(self, name):
            return getattr(numpy, name)

        def ndarray(self, shape, dtype=float, **kwargs):
            if kwargs:
                raise ValueError('Unsupported UVR band allocation arguments')
            return numpy.zeros(shape=shape, dtype=dtype)

    combine_globals = dict(combine.__globals__, np=NumpyProxy())
    safe_combine = types.FunctionType(combine.__code__, combine_globals, combine.__name__, combine.__defaults__, combine.__closure__)

    class SpecProxy:
        cmb_spectrogram_to_wave = staticmethod(safe_combine)

        def __getattr__(self, name):
            return getattr(spec_utils, name)

    path_globals = dict(original.__globals__, spec_utils=SpecProxy())
    path_method = types.FunctionType(original.__code__, path_globals, original.__name__, original.__defaults__, original.__closure__)
    instance._path_audio_ = types.MethodType(path_method, instance)
