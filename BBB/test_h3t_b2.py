import unittest

from strategy_cells import (
    classify_tail_accel_delta,
    tail_trail_bps_binding,
)


class H3TB2TailTests(unittest.TestCase):
    def test_delta_classifier(self):
        self.assertEqual(classify_tail_accel_delta(5.0, 4.5), "ACCEL")
        self.assertEqual(classify_tail_accel_delta(5.0, 4.9), "FLAT")
        self.assertEqual(classify_tail_accel_delta(5.0, 6.0), "DECEL")
        self.assertEqual(classify_tail_accel_delta(-0.5, -2.0), "DECEL")
        self.assertEqual(classify_tail_accel_delta(0.1, -0.5), "ACCEL")
        self.assertEqual(classify_tail_accel_delta(0.0, 1.0), "DECEL")

    def test_binding_is_off_before_first_milestone(self):
        self.assertEqual(
            tail_trail_bps_binding(34.0, 40.0, 15.3, []),
            34.0,
        )

    def test_binding_caps_reachable_fast_accel_after_second_milestone(self):
        # FAST TP=85. At MFE=130, milestones 1.0x and 1.5x are both reachable.
        # Floor=0.85*85=72.25, headroom=57.75. Raw ACCEL trail=0.40*130=52.
        # Headroom is wider, so no cap yet.
        self.assertAlmostEqual(
            tail_trail_bps_binding(52.0, 130.0, 72.25, [0, 1]),
            52.0,
        )
        # At the 2.0x milestone MFE=170, floor=1.30*85=110.5.
        # Raw ACCEL=68.0; headroom=59.5, so binding must cap to 59.5.
        self.assertAlmostEqual(
            tail_trail_bps_binding(68.0, 170.0, 110.5, [0, 1, 2]),
            59.5,
        )

    def test_transition_smoke(self):
        state = {
            "tail_m_prev": 3.0,
            "tail_delta_m": 0.0,
            "tail_max_delta_m": 0.0,
            "tail_accel": "FLAT",
            "tail_accel_seen": set(),
            "tail_accel_ticks": {"ACCEL": 0, "FLAT": 0, "DECEL": 0},
            "tail_transitions": 0,
        }
        for m_now in [3.5, 4.2, 4.1, 3.0, 1.5]:
            m_prev = state["tail_m_prev"]
            accel = classify_tail_accel_delta(m_now, m_prev)
            if state["tail_accel"] != accel and sum(state["tail_accel_ticks"].values()) > 0:
                state["tail_transitions"] += 1
            state["tail_accel"] = accel
            state["tail_accel_seen"].add(accel)
            state["tail_accel_ticks"][accel] += 1
            delta = m_now - m_prev
            state["tail_m_prev"] = m_now
            state["tail_delta_m"] = delta
            if abs(delta) > abs(state["tail_max_delta_m"]):
                state["tail_max_delta_m"] = delta

        self.assertEqual(state["tail_accel_seen"], {"ACCEL", "FLAT", "DECEL"})
        self.assertEqual(state["tail_accel_ticks"], {"ACCEL": 2, "FLAT": 1, "DECEL": 2})
        self.assertEqual(state["tail_transitions"], 2)
        self.assertAlmostEqual(state["tail_max_delta_m"], -1.5)


if __name__ == "__main__":
    unittest.main()
