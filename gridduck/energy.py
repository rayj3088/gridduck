from enum import Enum
from dataclasses import dataclass


class Band(Enum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"


@dataclass
class EnergyModel:
    green_threshold_price: float = 50.0
    red_threshold_price: float = 150.0
    green_threshold_load: float = 0.5
    red_threshold_load: float = 0.85

    def get_band(self, price: float, load_factor: float) -> Band:
        if price >= self.red_threshold_price or load_factor >= self.red_threshold_load:
            return Band.RED
        elif price >= self.green_threshold_price or load_factor >= self.green_threshold_load:
            return Band.YELLOW
        return Band.GREEN
