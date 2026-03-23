from .tp_nesting_kernel_guillotine import TpNestingKernelGuillotine
from .tp_nesting_kernel_maxrects import TpNestingKernelMaxRects
from .tp_nesting_kernel_skyline import TpNestingKernelSkyline


def get_nesting_kernel(kernel_name, *, kerf_mm):
    registry = {
        "guillotine": TpNestingKernelGuillotine,
        "maxrects": TpNestingKernelMaxRects,
        "skyline": TpNestingKernelSkyline,
    }
    kernel_cls = registry.get(kernel_name) or TpNestingKernelMaxRects
    return kernel_cls(kerf_mm=kerf_mm)
