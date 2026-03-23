class TpNestingPolicy:
    """Policy-aware scoring profile for optimizer plan selection."""

    PRESET_WEIGHTS = {
        "yield_first": {
            "waste_priority": 1.4,
            "offcut_reuse_priority": 0.7,
            "sheet_count_penalty": 1.2,
            "cost_sensitivity": 0.4,
        },
        "cost_first": {
            "waste_priority": 0.8,
            "offcut_reuse_priority": 0.3,
            "sheet_count_penalty": 1.0,
            "cost_sensitivity": 1.6,
        },
        "offcut_first": {
            "waste_priority": 0.8,
            "offcut_reuse_priority": 1.8,
            "sheet_count_penalty": 1.0,
            "cost_sensitivity": 0.5,
        },
    }

    VALID_PRESETS = ("yield_first", "cost_first", "offcut_first")

    def __init__(
        self,
        *,
        preset="yield_first",
        waste_priority=1.0,
        offcut_reuse_priority=1.0,
        sheet_count_penalty=1.0,
        cost_sensitivity=1.0,
    ):
        self.preset = preset if preset in self.PRESET_WEIGHTS else "yield_first"
        base = self.PRESET_WEIGHTS[self.preset]
        self.weights = {
            "waste_priority": float(base["waste_priority"]) * max(0.0, float(waste_priority or 0.0)),
            "offcut_reuse_priority": float(base["offcut_reuse_priority"])
            * max(0.0, float(offcut_reuse_priority or 0.0)),
            "sheet_count_penalty": float(base["sheet_count_penalty"]) * max(0.0, float(sheet_count_penalty or 0.0)),
            "cost_sensitivity": float(base["cost_sensitivity"]) * max(0.0, float(cost_sensitivity or 0.0)),
        }

    def score(self, *, waste_norm, reuse_norm, sheet_count, cost_norm):
        return (
            self.weights["waste_priority"] * float(waste_norm)
            + self.weights["sheet_count_penalty"] * float(sheet_count)
            + self.weights["cost_sensitivity"] * float(cost_norm)
            - self.weights["offcut_reuse_priority"] * float(reuse_norm)
        )

