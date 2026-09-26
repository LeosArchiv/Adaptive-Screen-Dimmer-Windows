from dimmer.gamma import build_ramp, channel_factors


def test_zero_strength_is_identity():
    assert channel_factors((255, 180, 100), 0) == (1.0, 1.0, 1.0)


def test_full_strength_is_colour():
    r, g, b = channel_factors((255, 153, 51), 100)
    assert r == 1.0 and abs(g - 0.6) < 1e-9 and abs(b - 0.2) < 1e-9


def test_ramp_keeps_black_black():
    ramp = build_ramp((1.0, 0.8, 0.6))
    assert ramp[0] == ramp[256] == ramp[512] == 0
    assert ramp[255] == 65535 and ramp[511] == round(65535 * 0.8)
