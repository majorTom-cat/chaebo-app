"""diffq 스텁 (chaebo SP-1).

diffq 는 cp311 win_amd64 휠이 없고 소스 빌드는 MSVC 를 요구한다.
demucs.states 가 최상단에서 임포트하지만 실사용은 '양자화된' 체크포인트 로드 시뿐 —
chaebo 는 비양자화 htdemucs_6s 만 MSST 경로(torch.load)로 로드하므로 안전.
실수로 양자화 경로를 타면 조용히 깨지지 않도록 명시적 에러를 낸다.
"""


class _DiffqUnavailable:
    def __init__(self, *args, **kwargs):
        raise RuntimeError(
            "diffq stub: quantized checkpoints are not supported in this environment "
            "(no cp311 wheel; install MSVC build tools or use python 3.10 if needed)"
        )


DiffQuantizer = _DiffqUnavailable
UniformQuantizer = _DiffqUnavailable


def restore_quantized_state(*args, **kwargs):
    raise RuntimeError(
        "diffq stub: quantized checkpoints are not supported in this environment"
    )
