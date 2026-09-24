import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class CurtailmentOrder:
    order_id: str
    target_reduction_pct: float
    duration_seconds: int
    issued_at: float = 0.0

    def __post_init__(self):
        if self.issued_at == 0.0:
            self.issued_at = time.time()

    @property
    def is_expired(self) -> bool:
        return time.time() > (self.issued_at + self.duration_seconds)


@dataclass
class GridSignal:
    load_factor: float  # 0.0 to 1.0 (1.0 = peak load)
    carbon_intensity_g_kwh: float  # gCO2/kWh
    electricity_price_mwh: float  # $ per MWh
    curtailment: Optional[CurtailmentOrder] = None
    timestamp: float = 0.0

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()


class SignalSource:
    def get_signal(self) -> GridSignal:
        raise NotImplementedError


# Alias for compatibility
BaseSignalSource = SignalSource


class StaticSource(SignalSource):
    def __init__(self, load_factor: float = 0.2, carbon_intensity: float = 150.0, price: float = 45.0):
        self.load_factor = load_factor
        self.carbon_intensity = carbon_intensity
        self.price = price
        self.active_curtailment: Optional[CurtailmentOrder] = None

    def set_curtailment(self, order: Optional[CurtailmentOrder]):
        self.active_curtailment = order

    def get_signal(self) -> GridSignal:
        if self.active_curtailment and self.active_curtailment.is_expired:
            self.active_curtailment = None

        return GridSignal(
            load_factor=self.load_factor,
            carbon_intensity_g_kwh=self.carbon_intensity,
            electricity_price_mwh=self.price,
            curtailment=self.active_curtailment,
            timestamp=time.time()
        )


# Alias for compatibility
StaticSignalSource = StaticSource


class WebhookSource(SignalSource):
    def __init__(self, initial_signal: Optional[GridSignal] = None):
        self._current_signal = initial_signal or GridSignal(
            load_factor=0.2,
            carbon_intensity_g_kwh=150.0,
            electricity_price_mwh=45.0
        )

    def update_signal(self, signal: GridSignal):
        self._current_signal = signal

    def get_signal(self) -> GridSignal:
        return self._current_signal
