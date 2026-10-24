from importlib.metadata import version
try:
    __version__ = version(__name__)
except:
    pass

from silero_vad.model import load_silero_vad
from silero_vad.utils_vad import (get_speech_timestamps,
                                  get_speech_timestamps_from_probs,
                                  save_audio,
                                  read_audio,
                                  VADIterator,
                                  collect_chunks,
                                  drop_chunks)

# The sequence path needs numpy and onnxruntime, which are optional extras, so
# it is imported on first access instead of at package import time (PEP 562).
_LAZY_SEQUENCE_ATTRS = ('SileroVADSequence', 'get_speech_timestamps_sequence')

__all__ = ['load_silero_vad',
           'get_speech_timestamps',
           'get_speech_timestamps_from_probs',
           'save_audio',
           'read_audio',
           'VADIterator',
           'collect_chunks',
           'drop_chunks',
           *_LAZY_SEQUENCE_ATTRS]


def __getattr__(name):
    if name in _LAZY_SEQUENCE_ATTRS:
        from silero_vad import sequence_vad
        return getattr(sequence_vad, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(__all__)
