"""开门探测 / 共享代理池平台配置（存 platform_settings.sell_open_probe_json）。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from typing import Any, Dict, Optional


@dataclass
class SellOpenProbeConfig:
    # 总开关：开售后走侦察兵 + Event 拉闸；关则退回纯时钟
    probe_enabled: bool = True
    # HotWindow 从共享售卖池借还代理（关则仍用用户绑定出口）
    shared_sell_pool_enabled: bool = True

    probe_user_id: Optional[int] = None
    probe_parallel: int = 2
    probe_round_gap_ms: int = 40
    probe_per_proxy_max_hits: int = 2
    probe_window_seconds: int = 120

    open_timeout_default_ms: int = 350
    open_timeout_min_ms: int = 250
    open_timeout_max_ms: int = 500
    open_timeout_margin_ms: int = 50

    probe_cooldown_default_ms: int = 3000
    probe_cooldown_min_ms: int = 2000
    probe_cooldown_max_ms: int = 5000

    sell_cooldown_default_ms: int = 11000
    sell_proxy_reserve: int = 4
    clock_fallback_ms: int = 2000

    calibration_enabled: bool = False


_CONFIG_KEYS = {f.name for f in fields(SellOpenProbeConfig)}


def default_probe_config() -> SellOpenProbeConfig:
    return SellOpenProbeConfig()


def parse_probe_config(raw: Any) -> SellOpenProbeConfig:
    base = default_probe_config()
    if raw is None:
        return base
    data: Dict[str, Any]
    if isinstance(raw, str):
        s = (raw or "").strip() or "{}"
        try:
            data = json.loads(s)
        except Exception:
            return base
    elif isinstance(raw, dict):
        data = raw
    else:
        return base
    if not isinstance(data, dict):
        return base
    kwargs: Dict[str, Any] = {}
    for k, v in data.items():
        if k not in _CONFIG_KEYS:
            continue
        kwargs[k] = v
    try:
        cfg = SellOpenProbeConfig(**{**asdict(base), **kwargs})
    except TypeError:
        cfg = base
    return _sanitize(cfg)


def _clamp_int(v: Any, lo: int, hi: int, default: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def _sanitize(cfg: SellOpenProbeConfig) -> SellOpenProbeConfig:
    cfg.probe_parallel = _clamp_int(cfg.probe_parallel, 1, 8, 2)
    cfg.probe_round_gap_ms = _clamp_int(cfg.probe_round_gap_ms, 10, 2000, 40)
    cfg.probe_per_proxy_max_hits = _clamp_int(cfg.probe_per_proxy_max_hits, 1, 5, 2)
    cfg.probe_window_seconds = _clamp_int(cfg.probe_window_seconds, 10, 600, 120)

    cfg.open_timeout_min_ms = _clamp_int(cfg.open_timeout_min_ms, 100, 2000, 250)
    cfg.open_timeout_max_ms = _clamp_int(cfg.open_timeout_max_ms, cfg.open_timeout_min_ms, 5000, 500)
    cfg.open_timeout_default_ms = _clamp_int(
        cfg.open_timeout_default_ms,
        cfg.open_timeout_min_ms,
        cfg.open_timeout_max_ms,
        350,
    )
    cfg.open_timeout_margin_ms = _clamp_int(cfg.open_timeout_margin_ms, 0, 500, 50)

    cfg.probe_cooldown_min_ms = _clamp_int(cfg.probe_cooldown_min_ms, 500, 30000, 2000)
    cfg.probe_cooldown_max_ms = _clamp_int(
        cfg.probe_cooldown_max_ms, cfg.probe_cooldown_min_ms, 60000, 5000
    )
    cfg.probe_cooldown_default_ms = _clamp_int(
        cfg.probe_cooldown_default_ms,
        cfg.probe_cooldown_min_ms,
        cfg.probe_cooldown_max_ms,
        3000,
    )

    cfg.sell_cooldown_default_ms = _clamp_int(cfg.sell_cooldown_default_ms, 1000, 60000, 11000)
    cfg.sell_proxy_reserve = _clamp_int(cfg.sell_proxy_reserve, 0, 200, 4)
    cfg.clock_fallback_ms = _clamp_int(cfg.clock_fallback_ms, 0, 60000, 2000)

    if cfg.probe_user_id is not None:
        try:
            cfg.probe_user_id = int(cfg.probe_user_id)
        except (TypeError, ValueError):
            cfg.probe_user_id = None
        if cfg.probe_user_id is not None and cfg.probe_user_id <= 0:
            cfg.probe_user_id = None

    cfg.probe_enabled = bool(cfg.probe_enabled)
    cfg.shared_sell_pool_enabled = bool(cfg.shared_sell_pool_enabled)
    cfg.calibration_enabled = bool(cfg.calibration_enabled)
    return cfg


def probe_config_to_dict(cfg: SellOpenProbeConfig) -> Dict[str, Any]:
    return asdict(_sanitize(cfg))


def probe_config_to_json(cfg: SellOpenProbeConfig) -> str:
    return json.dumps(probe_config_to_dict(cfg), ensure_ascii=False, separators=(",", ":"))


def effective_open_timeout_ms(cfg: SellOpenProbeConfig, baseline_p95_ms: Optional[float]) -> int:
    if baseline_p95_ms is None or baseline_p95_ms <= 0:
        return int(cfg.open_timeout_default_ms)
    raw = float(baseline_p95_ms) + float(cfg.open_timeout_margin_ms)
    return int(max(cfg.open_timeout_min_ms, min(cfg.open_timeout_max_ms, raw)))


def effective_probe_cooldown_ms(cfg: SellOpenProbeConfig, suggested_ms: Optional[float]) -> int:
    if suggested_ms is None or suggested_ms <= 0:
        return int(cfg.probe_cooldown_default_ms)
    return int(
        max(cfg.probe_cooldown_min_ms, min(cfg.probe_cooldown_max_ms, float(suggested_ms)))
    )
