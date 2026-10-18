from guardian.fusion import ACK_TEXT, CHECKIN_TEXT, CLEAR_TEXT, Fusion, FusionConfig, State


def kinds(fx):
    return [e.kind for e in fx]


def make():
    return Fusion(FusionConfig(checkin_s=20, verify_timeout_s=15, settle_s=1.5, recheck_s=4, rearm_s=10))


def speaks(fx):
    return [e.data["text"] for e in fx if e.kind == "speak"]


def watch(fx):
    return [e.data["type"] for e in fx if e.kind == "watch"]


def test_thud_then_person_stays_down_alerts_family():
    f = make()
    fx = f.on_trigger(100.0, "sound", level_db=-12.0)
    assert f.state == State.VERIFYING and watch(fx) == ["checking"]
    assert f.tick(101.0) == []                                  # settling after the thud
    assert kinds(f.tick(101.6)) == ["verify"]
    fx = f.on_vision(102.6, True, "Yes, a person is lying on the floor.")
    assert f.state == State.CHECKIN and speaks(fx) == [CHECKIN_TEXT]
    assert kinds(f.tick(106.7)) == ["verify"]                   # recheck while asking
    assert f.on_vision(107.7, True) == []                       # still down
    fx = f.tick(122.7)
    assert f.state == State.ALERT
    assert any(e.kind == "watch" and e.data["type"] == "alert" and "lying" in e.data["text"] for e in fx)
    assert any(e.kind == "log" and e.data["kind"] == "family_alert" for e in fx)


def test_motion_trigger_checks_immediately():
    f = make()
    f.on_trigger(0.0, "motion")
    assert kinds(f.tick(0.25)) == ["verify"]


def test_person_upright_is_a_false_alarm_and_rearms():
    f = make()
    f.on_trigger(0.0, "sound")
    f.tick(2.0)
    fx = f.on_vision(3.0, False, "No, the person is standing.")
    assert f.state == State.MONITORING and watch(fx) == ["clear"]
    assert any(e.data.get("kind") == "false_alarm_rejected" for e in fx if e.kind == "log")
    assert f.on_trigger(5.0, "sound")[0].data["reason"] == "rearm"
    assert f.on_trigger(14.0, "sound")[0].data["state"] == "VERIFYING"


def test_person_gets_up_during_checkin_or_alert_clears():
    for alert_first in (False, True):
        f = make()
        f.on_trigger(0.0, "motion")
        f.tick(0.1)
        f.on_vision(1.0, True)
        if alert_first:
            f.tick(21.5)
            assert f.state == State.ALERT
        fx = f.on_vision(25.0, False)
        assert f.state == State.MONITORING and f.incident is None
        assert speaks(fx) == [CLEAR_TEXT] and watch(fx) == ["clear"]


def test_camera_cannot_tell_still_asks_and_late_answers_are_rechecks():
    f = make()
    f.on_trigger(0.0, "sound")
    f.tick(2.0)
    f.on_vision(3.0, None, "camera view too dark to check")
    assert f.state == State.CHECKIN
    g = make()
    g.on_trigger(0.0, "sound")
    g.tick(2.0)
    g.tick(15.5)
    assert g.state == State.CHECKIN


def test_family_ack_clears_and_tells_the_person():
    f = make()
    f.on_trigger(0.0, "motion")
    f.tick(0.1)
    f.on_vision(1.0, True)
    f.tick(21.5)
    fx = f.on_family_ack(30.0)
    assert f.state == State.MONITORING and speaks(fx) == [ACK_TEXT]
    assert f.on_family_ack(31.0) == []
    assert f.on_trigger(32.0, "sound")[0].data["state"] == "VERIFYING"   # no rearm after a real incident
