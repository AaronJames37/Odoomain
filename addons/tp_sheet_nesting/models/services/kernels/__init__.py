from .tp_nesting_kernel_guillotine import TpNestingKernelGuillotine
from .tp_nesting_kernel_maxrects import TpNestingKernelMaxRects
from .tp_nesting_kernel_skyline import TpNestingKernelSkyline


def get_nesting_kernel(kernel_name, *, kerf_mm, guillotine_rip_first=False):
    registry = {
        "guillotine": TpNestingKernelGuillotine,
        "maxrects": TpNestingKernelMaxRects,
        "skyline": TpNestingKernelSkyline,
    }
    kernel_cls = registry.get(kernel_name) or TpNestingKernelMaxRects
    if kernel_cls is TpNestingKernelGuillotine:
        return kernel_cls(kerf_mm=kerf_mm, rip_first=guillotine_rip_first)
    return kernel_cls(kerf_mm=kerf_mm)
