"""Modified excerpts from emotional-memory (MIT).

Copyright (c) 2026 Gianluca Mazza <info@gianlucamazza.it>
Source: github.com/gianlucamazza/emotional-memory
Commit: 8b84bf3725448f2545337c08e346434819c01628
Original: mood.py MoodField.regress/update; affect.py CoreAffect.lerp/distance.
Changes: pure tuple functions; explicit elapsed time; caller owns validation,
serialization, clocks, provenance and persistence. No upstream store/LLM runs.
Full license: distributed _vendor/upstream/Emotional-Memory-LICENSE.
"""
from __future__ import annotations

import math


def regress_pad(current, baseline, elapsed, half_life, inertia=0.0):
    effective_half_life = half_life * (1.0 + inertia * 2.0)
    factor = math.exp(-elapsed * math.log(2) / effective_half_life)
    return tuple(base + (value - base) * factor for value, base in zip(current, baseline, strict=True))


def lerp_pad(current, target, alpha):
    return tuple(value + alpha * (other - value) for value, other in zip(current, target, strict=True))


def update_mood(current, target, alpha, inertia):
    effective_alpha = alpha * (1.0 - inertia)
    return tuple((1.0 - effective_alpha) * value + effective_alpha * other
                 for value, other in zip(current, target, strict=True))


def pad_distance(left, right):
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right, strict=True)))
