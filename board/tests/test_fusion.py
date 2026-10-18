from guardian.fusion import Fusion, FusionConfig, State


def kinds(fx):
    return [e.kind for e in fx]


def make():
    return Fusion(FusionConfig(checkin_s=20, verify_timeout_s=8, settle_s=1.5, floor_confirmations=2, rearm_s=10))


def test_impact_confirmed_by_camera_then_no_response_alerts():
    f = make()
    assert f.on_impact(100.0, 3.2)[0].data["state"] == "VERIFYING"
    assert f.tick(101.0) == []                       # still settling
    assert kinds(f.tick(101.6)) == ["verify"]
    fx = f.on_vision(103.0, True, "Person on floor")
    assert f.state == State.CHECKIN
    assert {"watch", "speak"} <= set(kinds(fx))
    assert f.tick(122.0) == []                       # 19 s into the check-in
    fx = f.tick(123.1)
    assert f.state == State.ALERT
    assert any(e.kind == "log" and e.data["kind"] == "caregiver_alert" for e in fx)


def test_camera_sees_person_upright_rejects_false_alarm_and_rearms():
    f = make()
    f.on_impact(0.0, 4.0)
    f.tick(2.0)
    fx = f.on_vision(3.0, False, "Person standing")
    assert f.state == State.MONITORING
    assert any(e.data.get("kind") == "false_alarm_rejected" for e in fx if e.kind == "log")
    assert f.on_impact(5.0, 4.0)[0].data["reason"] == "rearm"      # watch dropped again right away
    assert f.on_impact(14.0, 4.0)[0].data["state"] == "VERIFYING"


def test_wearer_ok_clears_checkin():
    f = make()
    f.on_impact(0.0)
    f.tick(2.0)
    f.on_vision(3.0, True)
    fx = f.on_ok(5.0)
    assert f.state == State.MONITORING and f.incident is None
    assert any(e.kind == "watch" and e.data["type"] == "clear" for e in fx)


def test_failed_or_slow_camera_still_asks_the_wearer():
    f = make()
    f.on_impact(0.0)
    f.tick(2.0)
    f.on_vision(3.0, None, "runner error")
    assert f.state == State.CHECKIN
    g = make()
    g.on_impact(0.0)
    g.tick(2.0)
    g.tick(8.5)
    assert g.state == State.CHECKIN
    g.on_vision(9.0, False)                          # late answer is ignored once asking
    assert g.state == State.CHECKIN


def test_camera_only_fall_needs_two_background_confirmations():
    f = make()
    assert f.on_vision(0.0, True, background=True) == []
    assert f.on_vision(30.0, False, background=True) == []
    f.on_vision(60.0, True, background=True)
    assert f.state == State.MONITORING
    f.on_vision(90.0, True, "on floor", background=True)
    assert f.state == State.CHECKIN and f.incident["source"] == "camera"


def test_sos_alerts_immediately_and_caregiver_can_resolve():
    f = make()
    f.on_sos(0.0)
    assert f.state == State.ALERT and f.incident["source"] == "sos"
    fx = f.on_ok(30.0, by="caregiver")
    assert f.state == State.MONITORING
    assert not any(e.kind == "speak" for e in fx)
