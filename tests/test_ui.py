"""Deterministic UI tests: hit areas, hover/pin/click-away state machine and island hover, on an offscreen display."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from claude_pulse import card as card_module  # noqa: E402
from claude_pulse import island as island_module  # noqa: E402
from claude_pulse import mascot, tray_icon  # noqa: E402
from claude_pulse import app as app_module  # noqa: E402
from claude_pulse.app import Controller  # noqa: E402
from claude_pulse.winapi import trim_working_set as REAL_TRIM  # noqa: E402
from claude_pulse.activity import DONE, IDLE, NEEDS_INPUT, RUNNING, SessionActivity, SessionMonitor  # noqa: E402
from claude_pulse.card import HoverCard  # noqa: E402
from claude_pulse.demo import apply_demo_data  # noqa: E402
from claude_pulse.island import Island  # noqa: E402
from claude_pulse.settings import AccountConfig, Settings  # noqa: E402
from claude_pulse.store import UsageStore  # noqa: E402
from claude_pulse.tween import Tweener  # noqa: E402

ICON = QRect(1200, 816, 32, 48)
FAR = QPoint(300, 300)


def press(widget: object, point: QPointF) -> None:
    event = QMouseEvent(
        QEvent.Type.MouseButtonPress, point, point, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    widget.mousePressEvent(event)  # type: ignore[attr-defined]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class UiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        accounts = [AccountConfig('team', Path('a')), AccountConfig('pro', Path('b'))]
        self.store = UsageStore(accounts, Settings(Path(self.folder.name) / 'settings.json'))
        apply_demo_data(self.store)
        self.clock = FakeClock()

    def tearDown(self) -> None:
        self.folder.cleanup()


class CardTests(UiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.card = HoverCard(self.store, Tweener(), use_24h=True)
        self.card._remeasure()
        self.card.grab()  # paints once so the hit areas exist
        patcher = mock.patch.object(card_module.time, 'monotonic', self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.card.hide)

    def _centre(self, kind: str, payload: object = None) -> QPointF:
        for rect, hit_kind, hit_payload in self.card._hits:
            if hit_kind == kind and (payload is None or hit_payload == payload):
                return rect.center()
        self.fail(f'no hit area {kind}/{payload}')

    def test_hit_areas_exist(self) -> None:
        kinds = [kind for _rect, kind, _payload in self.card._hits]
        self.assertEqual(kinds.count('account'), 2)
        self.assertEqual(kinds.count('island_mode'), 3)
        self.assertIn('refresh', kinds)

    def test_clicking_a_segment_selects_that_account(self) -> None:
        press(self.card, self._centre('account', 'pro'))
        self.assertEqual(self.store.active_id, 'pro')
        self.card._remeasure()
        self.card.grab()
        press(self.card, self._centre('account', 'team'))
        self.assertEqual(self.store.active_id, 'team')

    def test_island_mode_segments_emit_their_own_mode(self) -> None:
        received: list[str] = []
        self.card.island_mode_selected.connect(received.append)
        for mode in ('hover', 'always', 'off'):
            press(self.card, self._centre('island_mode', mode))
        self.assertEqual(received, ['hover', 'always', 'off'])

    def test_selected_mode_is_remembered_for_drawing(self) -> None:
        self.card.set_island_mode('hover')
        self.assertEqual(self.card._island_mode, 'hover')
        self.assertFalse(self.card.grab().isNull())

    def test_refresh_button_emits(self) -> None:
        received: list[bool] = []
        self.card.refresh_requested.connect(lambda: received.append(True))
        press(self.card, self._centre('refresh'))
        self.assertEqual(received, [True])

    def test_right_click_does_nothing(self) -> None:
        event = QMouseEvent(
            QEvent.Type.MouseButtonPress, self._centre('island_mode', 'always'), self._centre('island_mode', 'always'),
            Qt.MouseButton.RightButton, Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier,
        )
        received: list[str] = []
        self.card.island_mode_selected.connect(received.append)
        self.card.mousePressEvent(event)
        self.assertEqual(received, [])

    def test_hover_needs_a_dwell_then_shows(self) -> None:
        over_icon = ICON.center()
        self.card.tick(over_icon, ICON, False)
        self.assertFalse(self.card.isVisible())
        self.clock.now += 0.05
        self.card.tick(over_icon, ICON, False)
        self.assertFalse(self.card.isVisible())
        self.clock.now += 0.10
        self.card.tick(over_icon, ICON, False)
        self.assertTrue(self.card.isVisible())
        self.assertFalse(self.card.pinned)

    def test_brushing_past_the_icon_does_not_show(self) -> None:
        self.card.tick(ICON.center(), ICON, False)
        self.clock.now += 0.05
        self.card.tick(FAR, ICON, False)
        self.clock.now += 1.0
        self.card.tick(FAR, ICON, False)
        self.assertFalse(self.card.isVisible())

    def test_card_hides_after_pointer_leaves_but_not_while_on_card(self) -> None:
        self.card.present(ICON, ICON.center(), pinned=False)
        on_card = self.card.body_global_rect().center()
        self.card.tick(on_card, ICON, False)
        self.clock.now += 5
        self.card.tick(on_card, ICON, False)
        self.assertTrue(self.card._fade.endValue() != 0.0)

        self.card.tick(FAR, ICON, False)
        self.clock.now += 0.1
        self.card.tick(FAR, ICON, False)
        self.assertNotEqual(self.card._fade.endValue(), 0.0)
        self.clock.now += 0.5
        self.card.tick(FAR, ICON, False)
        self.assertEqual(self.card._fade.endValue(), 0.0)

    def test_pinned_card_survives_leaving_and_dismisses_on_outside_click(self) -> None:
        self.card.toggle_pinned(ICON, ICON.center())
        self.assertTrue(self.card.pinned)

        self.clock.now += 10
        self.card.tick(FAR, ICON, False)
        self.assertTrue(self.card.pinned)

        self.card.tick(FAR, ICON, True)  # press edge, outside, after the grace period
        self.assertFalse(self.card.pinned)
        self.assertEqual(self.card._fade.endValue(), 0.0)

    def test_click_inside_card_or_on_icon_keeps_it_pinned(self) -> None:
        self.card.toggle_pinned(ICON, ICON.center())
        self.clock.now += 10
        self.card.tick(self.card.body_global_rect().center(), ICON, True)
        self.assertTrue(self.card.pinned)
        self.card.tick(self.card.body_global_rect().center(), ICON, False)
        self.card.tick(ICON.center(), ICON, True)
        self.assertTrue(self.card.pinned)

    def test_the_pinning_click_itself_cannot_dismiss(self) -> None:
        self.card.toggle_pinned(QRect(), QPoint(900, 840))  # icon position unknown
        self.clock.now += 0.1
        self.card.tick(QPoint(900, 840), QRect(), True)
        self.assertTrue(self.card.pinned)

    def test_second_click_on_icon_unpins(self) -> None:
        self.card.toggle_pinned(ICON, ICON.center())
        self.card.toggle_pinned(ICON, ICON.center())
        self.assertFalse(self.card.pinned)

    def test_closed_card_does_not_pop_back_while_pointer_rests_on_icon(self) -> None:
        self.card.toggle_pinned(ICON, ICON.center())
        self.card.toggle_pinned(ICON, ICON.center())  # click again to close
        for _ in range(20):
            self.clock.now += 0.1
            self.card.tick(ICON.center(), ICON, False)
        self.assertEqual(self.card._fade.endValue(), 0.0)

        self.card.tick(FAR, ICON, False)  # leaving the icon re-arms hover
        self.card.tick(ICON.center(), ICON, False)
        self.clock.now += 0.2
        self.card.tick(ICON.center(), ICON, False)
        self.assertNotEqual(self.card._fade.endValue(), 0.0)

    def test_context_menu_hides_card_and_hover_stays_off_until_pointer_leaves(self) -> None:
        self.card.present(ICON, ICON.center(), pinned=False)
        self.card.hide_for_menu()
        for _ in range(10):
            self.clock.now += 0.1
            self.card.tick(ICON.center(), ICON, False)
        self.assertEqual(self.card._fade.endValue(), 0.0)

    def test_card_stays_inside_the_screen_above_a_bottom_taskbar(self) -> None:
        self.card.present(ICON, ICON.center(), pinned=True)
        body = self.card.body_global_rect()
        screen = self.application.primaryScreen()
        self.assertTrue(screen.availableGeometry().adjusted(-1, -1, 1, 1).contains(body) or body.bottom() <= screen.geometry().bottom())


class IslandTests(UiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.island = Island(self.store, Tweener())
        patcher = mock.patch.object(island_module.time, 'monotonic', self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.island.hide)
        self.island.set_enabled(True)
        self.collapsed = self.island._pill_rect(expanded=False).center()

    def test_opens_after_dwell_and_closes_after_leaving(self) -> None:
        self.island.tick(self.collapsed)
        self.assertFalse(self.island._target_open)
        self.clock.now += 0.3
        self.island.tick(self.collapsed)
        self.assertTrue(self.island._target_open)

        inside = self.island._pill_rect(expanded=True).center()
        self.clock.now += 5
        self.island.tick(inside)
        self.assertTrue(self.island._target_open)

        self.island.tick(FAR)
        self.clock.now += 0.2
        self.island.tick(FAR)
        self.assertTrue(self.island._target_open)
        self.clock.now += 0.5
        self.island.tick(FAR)
        self.assertFalse(self.island._target_open)

    def test_a_pointer_passing_through_does_not_open_it(self) -> None:
        self.island.tick(self.collapsed)
        self.clock.now += 0.1
        self.island.tick(FAR)
        self.clock.now += 1.0
        self.island.tick(self.collapsed)
        self.assertFalse(self.island._target_open)

    def test_disabled_or_suppressed_island_ignores_the_pointer(self) -> None:
        self.island.set_suppressed(True)
        self.island.tick(self.collapsed)
        self.clock.now += 1
        self.island.tick(self.collapsed)
        self.assertFalse(self.island._target_open)

        self.island.set_suppressed(False)
        self.island.set_enabled(False)
        self.island.tick(self.collapsed)
        self.clock.now += 1
        self.island.tick(self.collapsed)
        self.assertFalse(self.island._target_open)

    def test_panel_click_selects_account_once_expanded(self) -> None:
        self.island._expand = 1.0
        self.island._presence = 1.0
        self.island._target_open = True
        self.island._apply_geometry()
        self.island.grab()
        panels = [(rect, payload) for rect, kind, payload in self.island._hits if kind == 'account']
        self.assertEqual(len(panels), 2)
        rect, account_id = panels[1]
        press(self.island, rect.center())
        self.assertEqual(self.store.active_id, account_id)

    def test_spring_curve_is_quick_overshoots_a_little_and_lands_exactly(self) -> None:
        samples = [island_module._spring(step / 200) for step in range(201)]
        self.assertEqual(samples[0], 0.0)
        self.assertEqual(samples[-1], 1.0)
        self.assertGreater(samples[20], 0.3)  # already well on its way after a tenth of the time
        self.assertGreater(max(samples), 1.0)
        self.assertLess(max(samples), 1.08)  # never a wobble that would clip the window margin
        self.assertTrue(all(abs(value - 1.0) < 0.01 for value in samples[140:]))  # settled for the last 30%

    def test_animation_never_resizes_the_window(self) -> None:
        geometry = self.island.geometry()
        self.island._open()
        for fraction in (0.1, 0.4, 0.8, 1.0):
            self.island._on_motion(fraction)
            self.assertEqual(self.island.geometry(), geometry)
        self.island._close()
        for fraction in (0.3, 1.0):
            self.island._on_motion(fraction)
            self.assertEqual(self.island.geometry(), geometry)
        self.assertEqual(self.island._expand, 0.0)

    def test_motion_lands_exactly_on_its_target(self) -> None:
        self.island._open()
        self.island._on_motion(1.0)
        self.assertEqual(self.island._expand, 1.0)
        self.island._close()
        self.island._on_motion(1.0)
        self.assertEqual(self.island._expand, 0.0)

    def test_reversing_part_way_takes_proportionally_less_time(self) -> None:
        self.island._expand = 1.0
        self.island._run_motion(0.0, 340, island_module._ease_out)
        full = self.island._motion.duration()
        self.island._expand = 0.5
        self.island._run_motion(0.0, 340, island_module._ease_out)
        half = self.island._motion.duration()
        self.assertEqual(full, 340)
        self.assertLess(half, full)
        self.assertGreaterEqual(half, island_module._MIN_MOTION_MS)

    def test_reversal_starts_from_the_current_size_without_a_jump(self) -> None:
        self.island._open()
        self.island._on_motion(0.1)
        midway = self.island._expand
        self.island._close()
        self.island._on_motion(0.0)
        self.assertAlmostEqual(self.island._expand, midway, places=6)

    def test_expanded_size_grows_with_account_count(self) -> None:
        two = self.island._expanded_size()[0]
        accounts = [AccountConfig(name, Path(name)) for name in ('a', 'b', 'c')]
        three_store = UsageStore(accounts, Settings(Path(self.folder.name) / 'three.json'))
        self.assertGreater(Island(three_store, Tweener())._expanded_size()[0], two)


def session(state: str, key: str = 'one', account: str = 'team') -> SessionActivity:
    return SessionActivity(account, key, 'Project', 'A task', state, 'Editing a.py' if state == RUNNING else '', 1000.0)


class IslandModeTests(UiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.monitor = SessionMonitor([])
        self.island = Island(self.store, Tweener(), True, self.monitor)
        patcher = mock.patch.object(island_module.time, 'monotonic', self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.island.hide)
        screen = self.island._screen_geometry()
        self.zone = QPoint(screen.center().x(), screen.top())

    def _hover_edge(self, seconds: float = 0.3) -> None:
        self.island.tick(self.zone)
        self.clock.now += seconds
        self.island.tick(self.zone)

    def _leave(self, seconds: float) -> None:
        self.island.tick(FAR)
        self.clock.now += seconds
        self.island.tick(FAR)

    def test_hover_mode_starts_hidden_and_drops_when_the_pointer_touches_the_top_middle(self) -> None:
        self.island.set_mode('hover')
        self.assertFalse(self.island.isVisible())
        self.assertFalse(self.island._wants_visible())
        self._hover_edge()
        self.assertTrue(self.island._dropped)
        self.assertTrue(self.island.isVisible())

    def test_edge_must_be_dwelt_on_and_must_be_in_the_middle(self) -> None:
        self.island.set_mode('hover')
        self.island.tick(self.zone)
        self.clock.now += 0.05
        self.island.tick(self.zone)
        self.assertFalse(self.island._dropped)  # brushed past

        screen = self.island._screen_geometry()
        corner = QPoint(screen.left() + 5, screen.top())
        self.island.tick(corner)
        self.clock.now += 1.0
        self.island.tick(corner)
        self.assertFalse(self.island._dropped)  # the top edge, but not the middle

        below = QPoint(screen.center().x(), screen.top() + 40)
        self.island.tick(below)
        self.clock.now += 1.0
        self.island.tick(below)
        self.assertFalse(self.island._dropped)  # near, but not touching the edge

    def test_hover_mode_lifts_away_again_after_the_pointer_leaves(self) -> None:
        self.island.set_mode('hover')
        self._hover_edge()
        self.assertTrue(self.island._dropped)
        self._leave(0.5)
        self.assertTrue(self.island._dropped)  # grace period
        self.clock.now += 0.6
        self.island.tick(FAR)
        self.assertFalse(self.island._dropped)
        self.assertEqual(self.island._fade.endValue(), 0.0)

    def test_it_stays_while_the_pointer_rests_on_the_edge_zone_or_the_pill(self) -> None:
        self.island.set_mode('hover')
        self._hover_edge()
        for _ in range(30):
            self.clock.now += 0.2
            self.island.tick(self.zone)
        self.assertTrue(self.island._dropped)

        pill = self.island._pill_rect(expanded=False).center()
        for _ in range(30):
            self.clock.now += 0.2
            self.island.tick(pill)
        self.assertTrue(self.island._dropped)

    def test_always_mode_ignores_the_edge_and_never_lifts(self) -> None:
        self.island.set_mode('always')
        self.assertTrue(self.island.isVisible())
        self._hover_edge()
        self._leave(5.0)
        self.assertTrue(self.island._wants_visible())
        self.assertEqual(self.island._fade.endValue(), 1.0)

    def test_off_mode_never_shows_and_switching_modes_resets_the_drop(self) -> None:
        self.island.set_mode('off')
        self._hover_edge()
        self.assertFalse(self.island.isVisible())

        self.island.set_mode('hover')
        self._hover_edge()
        self.assertTrue(self.island._dropped)
        self.island.set_mode('hover')
        self.assertFalse(self.island._dropped)
        self.island.set_mode('nonsense')
        self.assertEqual(self.island.mode, 'off')

    def test_fullscreen_suppression_hides_a_dropped_island(self) -> None:
        self.island.set_mode('hover')
        self._hover_edge()
        self.island.set_suppressed(True)
        self.assertFalse(self.island._wants_visible())
        self.assertEqual(self.island._fade.endValue(), 0.0)

    def test_a_finished_task_makes_a_hidden_island_peek_out_for_a_few_seconds(self) -> None:
        self.island.set_mode('hover')
        self.monitor.set_fixed([session(RUNNING)])
        self.assertFalse(self.island._dropped)
        self.monitor.set_fixed([session(DONE)])
        self.assertTrue(self.island._dropped)

        self.clock.now += 2
        self._leave(1.5)
        self.assertTrue(self.island._dropped)  # still peeking
        self.clock.now += 5
        self.island.tick(FAR)
        self.island.tick(FAR)
        self.clock.now += 1.0
        self.island.tick(FAR)
        self.assertFalse(self.island._dropped)

    def test_needing_input_also_peeks_but_steady_states_do_not(self) -> None:
        self.island.set_mode('hover')
        self.monitor.set_fixed([session(IDLE)])
        self.monitor.set_fixed([session(DONE)])  # finished before we ever saw it running: no alert
        self.assertFalse(self.island._dropped)
        self.monitor.set_fixed([session(NEEDS_INPUT)])
        self.assertTrue(self.island._dropped)

    def test_always_mode_does_not_peek_or_toggle_on_activity(self) -> None:
        self.island.set_mode('always')
        before = self.island._dropped
        self.monitor.set_fixed([session(RUNNING)])
        self.monitor.set_fixed([session(DONE)])
        self.assertEqual(self.island._dropped, before)

    def test_activity_rows_make_the_open_island_taller_and_are_capped(self) -> None:
        self.island.set_show_activity(True)
        none_height = self.island._expanded_size(0)[1]
        self.monitor.set_fixed([session(RUNNING, 'a'), session(DONE, 'b'), session(IDLE, 'c'), session(IDLE, 'd')])
        self.island._refresh_rows()
        self.assertEqual(self.island._rows, 3)
        self.assertGreater(self.island._expanded_size()[1], none_height)
        self.assertEqual(self.island._expanded_size(3)[1] - self.island._expanded_size(2)[1], island_module.ROW_HEIGHT + island_module.ROW_GAP)

    def test_row_count_is_frozen_while_open(self) -> None:
        self.island.set_show_activity(True)
        self.monitor.set_fixed([session(RUNNING, 'a'), session(DONE, 'b')])
        self.island._refresh_rows()
        self.island._target_open = True
        self.monitor.set_fixed([session(RUNNING, 'a')])
        self.assertEqual(self.island._rows, 2)

    def test_turning_activity_off_removes_the_mascot_slot_and_rows(self) -> None:
        self.island.set_show_activity(True)
        wide = self.island._collapsed_width()
        self.monitor.set_fixed([session(RUNNING)])
        self.island.set_show_activity(False)
        self.assertEqual(self.island._collapsed_width(), wide - island_module.MASCOT_SLOT)
        self.assertEqual(self.island._rows, 0)
        self.assertEqual(self.island._sessions(), [])

    def test_window_is_sized_for_the_biggest_pill_and_never_changes_with_rows(self) -> None:
        self.island.set_show_activity(True)
        self.island.set_mode('always')
        geometry = self.island.geometry()
        self.monitor.set_fixed([session(RUNNING)])
        self.island._refresh_rows()
        self.island._apply_geometry()
        self.assertEqual(self.island.geometry(), geometry)
        self.assertGreaterEqual(geometry.height(), self.island._expanded_size(3)[1])

    def test_animation_clock_runs_only_while_a_task_is_running_on_screen(self) -> None:
        self.island.set_show_activity(True)
        self.island.set_mode('always')
        self.assertFalse(self.island._clock.isActive())
        self.monitor.set_fixed([session(RUNNING)])
        self.assertTrue(self.island._clock.isActive())
        self.monitor.set_fixed([session(DONE)])
        self.assertFalse(self.island._clock.isActive())
        self.monitor.set_fixed([session(RUNNING)])
        self.island.set_mode('off')
        self.island._fade.stop()
        self.island._on_fade_finished()
        self.assertFalse(self.island._clock.isActive())

    def test_animation_changes_pixels_only_inside_the_repaint_rectangle(self) -> None:
        """The collapsed pill repaints just the mascot's rectangle; every pixel that animates must be inside it."""
        self.island.set_show_activity(True)
        self.monitor.set_fixed([session(RUNNING)])
        for mode, presence in (('always', 1.0), ('hover', 0.55)):
            self.island._mode = mode
            self.island._presence = presence
            self.island._expand = 0.0
            self.island._apply_geometry()
            frames = []
            for moment in (10.0, 10.07, 10.14, 10.21, 10.33):
                self.clock.now = moment
                frames.append(self.island.grab().toImage())

            area = self.island._mascot_area()
            changed = [
                (x, y)
                for first, second in zip(frames, frames[1:])
                for y in range(first.height()) for x in range(first.width())
                if first.pixel(x, y) != second.pixel(x, y)
            ]
            self.assertTrue(changed, f'{mode}: the running mascot should animate')
            outside = [(x, y) for x, y in changed if not area.contains(x, y)]
            self.assertEqual(outside, [], f'{mode}: {len(outside)} animated pixels fall outside {area}')

    def test_repaint_area_follows_the_slide_in(self) -> None:
        self.island.set_show_activity(True)
        self.island._mode = 'hover'
        self.island._presence = 1.0
        settled = self.island._mascot_area()
        self.island._presence = 0.0
        hidden = self.island._mascot_area()
        self.assertEqual(settled.x(), hidden.x())
        self.assertLess(hidden.y(), settled.y())

    def test_painting_with_every_session_state_does_not_fail(self) -> None:
        self.island.set_show_activity(True)
        states = [(RUNNING,), (DONE, IDLE), (NEEDS_INPUT, RUNNING, DONE), ()]
        for combo in states:
            self.monitor.set_fixed([session(state, str(index)) for index, state in enumerate(combo)])
            self.island._refresh_rows()
            self.island._presence = 1.0
            for progress in (0.0, 0.5, 1.0):
                self.island._expand = progress
                self.island._target_open = progress > 0
                self.assertFalse(self.island.grab().isNull())


class AttachedStyleTests(UiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.monitor = SessionMonitor([])
        self.island = Island(self.store, Tweener(), True, self.monitor)
        patcher = mock.patch.object(island_module.time, 'monotonic', self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.island.hide)

    def _collapsed_image(self, style: str):
        self.island.set_style(style)
        self.island._presence = 1.0
        self.island._expand = 0.0
        self.island._apply_geometry()
        return self.island.grab().toImage()

    def _pill_left(self) -> float:
        return (self.island.width() - self.island._collapsed_width()) / 2

    def test_attached_pill_touches_the_top_edge_with_curved_ears_and_rounded_bottom(self) -> None:
        image = self._collapsed_image('attached')
        left, middle = round(self._pill_left()), self.island.width() // 2
        alpha = lambda x, y: image.pixelColor(x, y).alpha()  # noqa: E731
        self.assertGreater(alpha(middle, 0), 240)               # flush: the very first row is already pill
        self.assertGreater(alpha(left - 3, 1), 200)             # the ear: pill colour just outside the wall, at the top
        self.assertEqual(alpha(left - 9, 24), 0)                # ...but nothing beside the wall lower down
        self.assertEqual(alpha(left + 1, island_module.COLLAPSED_HEIGHT - 2), 0)  # rounded bottom corner
        self.assertGreater(alpha(middle, island_module.COLLAPSED_HEIGHT - 2), 240)

    def test_floating_pill_keeps_its_gap_and_shape(self) -> None:
        image = self._collapsed_image('floating')
        middle = self.island.width() // 2
        self.assertLess(image.pixelColor(middle, 0).alpha(), 40)
        self.assertGreater(image.pixelColor(middle, island_module.MARGIN_TOP + 10).alpha(), 240)

    def test_attached_window_starts_at_the_top_of_the_screen(self) -> None:
        self.island.set_style('attached')
        self.assertEqual(self.island.geometry().top(), self.island._screen_geometry().top())
        self.assertEqual(self.island._pill_rect(expanded=False).top(), self.island._screen_geometry().top())
        self.island.set_style('floating')
        self.assertEqual(self.island._pill_rect(expanded=False).top(), self.island._screen_geometry().top() + island_module.TOP_OFFSET)

    def test_notch_shape(self) -> None:
        rect = QRectF(100, 0, 200, 36)
        path = island_module.notch_path(rect, 16, 10)
        self.assertTrue(path.contains(QPointF(200, 1)))
        self.assertTrue(path.contains(QPointF(97, 1)))        # inside the left ear
        self.assertFalse(path.contains(QPointF(91, 20)))      # outside the wall
        self.assertFalse(path.contains(QPointF(101, 35)))     # cut-off bottom corner
        self.assertTrue(path.contains(QPointF(301, 1)) or path.contains(QPointF(303, 1)))  # the right ear mirrors the left
        outline = island_module.notch_path(rect, 16, 10, outline_only=True)
        self.assertLess(outline.elementCount(), path.elementCount())  # the open outline leaves out the closing top edge

    def test_resting_on_the_edge_does_not_open_an_attached_pill_but_entering_it_does(self) -> None:
        self.island.set_style('attached')
        self.island.set_mode('always')
        screen = self.island._screen_geometry()
        edge = QPoint(screen.center().x(), screen.top())
        self.island.tick(edge)
        self.clock.now += 1.0
        self.island.tick(edge)
        self.assertFalse(self.island._target_open)

        inside = self.island._pill_rect(expanded=False).center()
        self.island.tick(inside)
        self.clock.now += 0.3
        self.island.tick(inside)
        self.assertTrue(self.island._target_open)

    def test_hover_mode_drops_an_attached_island_without_opening_it_too(self) -> None:
        self.island.set_style('attached')
        self.island.set_mode('hover')
        screen = self.island._screen_geometry()
        edge = QPoint(screen.center().x(), screen.top())
        self.island.tick(edge)
        self.clock.now += 0.3
        self.island.tick(edge)
        self.assertTrue(self.island._dropped)
        for _ in range(10):
            self.clock.now += 0.2
            self.island.tick(edge)
        self.assertTrue(self.island._dropped)
        self.assertFalse(self.island._target_open)

    def test_hidden_attached_island_is_entirely_above_the_screen_edge(self) -> None:
        self.island.set_style('attached')
        self.island.set_mode('hover')
        self.island._presence = 0.0
        self.island._expand = 0.0
        image = self.island.grab().toImage()
        lit = [(x, y) for y in range(70) for x in range(image.width()) if image.pixelColor(x, y).alpha() > 0]
        self.assertEqual(lit, [])

    def test_unknown_style_falls_back_to_floating(self) -> None:
        self.island.set_style('diagonal')
        self.assertEqual(self.island.style, 'floating')

    def test_animation_changes_pixels_only_inside_the_repaint_rectangle_when_attached(self) -> None:
        self.island.set_show_activity(True)
        self.monitor.set_fixed([session(RUNNING)])
        self.island.set_style('attached')
        self.island._presence = 1.0
        self.island._expand = 0.0
        frames = []
        for moment in (10.0, 10.07, 10.14, 10.21, 10.33):
            self.clock.now = moment
            frames.append(self.island.grab().toImage())

        area = self.island._mascot_area()
        changed = [
            (x, y) for first, second in zip(frames, frames[1:])
            for y in range(first.height()) for x in range(first.width()) if first.pixel(x, y) != second.pixel(x, y)
        ]
        self.assertTrue(changed)
        self.assertEqual([(x, y) for x, y in changed if not area.contains(x, y)], [])

    def test_every_state_paints_attached(self) -> None:
        self.island.set_style('attached')
        self.island.set_show_activity(True)
        self.monitor.set_fixed([session(NEEDS_INPUT, 'a'), session(RUNNING, 'b'), session(DONE, 'c')])
        self.island._refresh_rows()
        self.island._presence = 1.0
        for progress in (0.0, 0.3, 0.7, 1.0, 1.04):
            self.island._expand = progress
            self.island._target_open = progress > 0
            self.assertFalse(self.island.grab().isNull())


class ControllerStyleTests(UiTestCase):
    def test_style_menu_item_and_setting_stay_in_step(self) -> None:
        settings = Settings(Path(self.folder.name) / 'controller.json')
        controller = Controller(self.store, settings, Path('run.pyw'))
        self.addCleanup(controller.island.hide)
        controller.set_island_style('floating')
        self.assertEqual((settings.island_style, controller.island.style, controller._attached_action.isChecked()), ('floating', 'floating', False))
        controller._attached_action.trigger()
        self.assertEqual((settings.island_style, controller.island.style, controller._attached_action.isChecked()), ('attached', 'attached', True))
        controller.set_island_style('floating', persist=False)
        self.assertEqual(settings.island_style, 'attached')
        self.assertFalse(controller._attached_action.isChecked())


class MemoryTrimTests(UiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.controller = Controller(self.store, Settings(Path(self.folder.name) / 'trim.json'), Path('run.pyw'), SessionMonitor([]))
        self.addCleanup(self.controller.island.hide)
        self.addCleanup(self.controller.card.hide)
        self.trims: list[int] = []
        for patcher in (
            mock.patch.object(app_module.winapi, 'trim_working_set', side_effect=lambda: self.trims.append(1) or True),
            mock.patch.object(app_module.time, 'monotonic', self.clock),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _idle_for(self, seconds: float, step: float = 1.5) -> None:
        end = self.clock.now + seconds
        while self.clock.now < end:
            self.controller._trim_when_idle()
            self.clock.now += step

    def test_trims_once_after_twenty_quiet_seconds(self) -> None:
        self._idle_for(15)
        self.assertEqual(self.trims, [])
        self._idle_for(15)
        self.assertEqual(len(self.trims), 1)
        self._idle_for(120)
        self.assertEqual(len(self.trims), 1)  # once per quiet spell, not on every housekeeping tick

    def test_activity_resets_the_wait_and_allows_a_later_trim(self) -> None:
        self._idle_for(30)
        self.assertEqual(len(self.trims), 1)
        self.controller.card._fade.setStartValue(0.0)
        self.controller.card._fade.setEndValue(1.0)
        self.controller.card._fade.start()  # the card starts fading in: busy
        self._idle_for(60)
        self.assertEqual(len(self.trims), 1)
        self.controller.card._fade.stop()
        self._idle_for(30)
        self.assertEqual(len(self.trims), 2)

    def test_never_trims_while_anything_is_on_screen_or_moving(self) -> None:
        self.controller.island.set_mode('always')
        self.controller.island._open()  # opening animation running
        self._idle_for(10)
        self.assertEqual(self.trims, [])
        self.controller.island._motion.stop()
        self.controller.island._expand = 0.5      # half open
        self._idle_for(60)
        self.assertEqual(self.trims, [])

    def test_a_running_task_animation_does_not_block_trimming(self) -> None:
        monitor = SessionMonitor([])
        self.controller.island.set_show_activity(True)
        self.controller.island.set_mode('always')
        self.controller.island._fade.stop()
        self.controller.island._presence = 1.0
        monitor.set_fixed([session(RUNNING)])
        self.controller.island._activity = monitor
        monitor.changed.connect(self.controller.island._on_activity_changed)
        monitor.set_fixed([session(RUNNING, 'two')])
        self.assertTrue(self.controller.island._clock.isActive())
        self.assertFalse(self.controller.island.is_busy())
        self._idle_for(30)
        self.assertEqual(len(self.trims), 1)

    def test_never_trims_while_a_fetch_is_in_flight(self) -> None:
        self.store._runtime[self.store.accounts[0].id].in_flight = True
        self._idle_for(90)
        self.assertEqual(self.trims, [])
        self.store._runtime[self.store.accounts[0].id].in_flight = False
        self._idle_for(30)
        self.assertEqual(len(self.trims), 1)

    def test_real_trim_call_works_and_shrinks_the_resident_set(self) -> None:
        import ctypes
        import sys

        if sys.platform != 'win32':
            self.skipTest('Windows only')
        ballast = [bytearray(2_000_000) for _ in range(10)]  # touch ~20 MB so there is something to give back
        for block in ballast:
            block[::4096] = b'x' * len(block[::4096])
        class Counters(ctypes.Structure):
            _fields_ = [('cb', ctypes.c_uint32), ('PageFaultCount', ctypes.c_uint32), ('Peak', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                        ('a', ctypes.c_size_t), ('b', ctypes.c_size_t), ('c', ctypes.c_size_t), ('d', ctypes.c_size_t), ('e', ctypes.c_size_t),
                        ('f', ctypes.c_size_t), ('PrivateUsage', ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL('kernel32')
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.K32GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_uint32]

        def resident() -> int:
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            kernel32.K32GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
            return counters.WorkingSetSize

        before = resident()
        self.addCleanup(lambda: ballast.clear())
        self.assertTrue(REAL_TRIM())  # the genuine function, captured before setUp replaced it with a recorder
        self.assertLess(resident(), before * 0.8)


class MascotTests(UiTestCase):
    def _draw(self, state: str, seconds: float = 0.5):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QImage, QPainter

        image = QImage(60, 40, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        mascot.draw_mascot(painter, QRectF(4, 4, 52, 32), state, seconds)
        painter.end()
        return image

    @staticmethod
    def _ink(image) -> int:
        return sum(image.pixelColor(x, y).alpha() for x in range(image.width()) for y in range(image.height()))

    def test_every_state_draws_something_and_idle_is_dimmer(self) -> None:
        inks = {state: self._ink(self._draw(state)) for state in (RUNNING, DONE, NEEDS_INPUT, IDLE)}
        self.assertTrue(all(value > 500 for value in inks.values()))
        self.assertLess(inks[IDLE], inks[RUNNING] * 0.7)
        self.assertGreater(inks[DONE], inks[RUNNING])  # the check badge adds ink

    def test_running_mascot_moves_and_idle_one_holds_still(self) -> None:
        self.assertNotEqual(self._draw(RUNNING, 0.30).constBits().tobytes(), self._draw(RUNNING, 0.45).constBits().tobytes())
        self.assertEqual(self._draw(IDLE, 0.30).constBits().tobytes(), self._draw(IDLE, 0.45).constBits().tobytes())


class RenderingTests(UiTestCase):
    def test_every_icon_size_and_state_renders_nonempty(self) -> None:
        for size in (16, 20, 24, 32, 48, 64):
            for session, weekly in ((0, 0), (42, 18), (70, 90), (100, 100), (None, None), (250, 5)):
                for light in (False, True):
                    pixmap = tray_icon.render_icon(size, session, weekly, 1, 2, light)
                    self.assertEqual(pixmap.width(), size)
                    image = pixmap.toImage()
                    opaque = sum(1 for x in range(size) for y in range(size) if image.pixelColor(x, y).alpha() > 0)
                    self.assertGreater(opaque, size, f'blank icon at {size}px {session}/{weekly}')

    def test_card_and_island_paint_in_every_data_state(self) -> None:
        card = HoverCard(self.store, Tweener(), use_24h=False)
        island = Island(self.store, Tweener())
        island._presence = 1.0
        for account in self.store.accounts:
            for error, keep_data in ((None, True), ('network', True), ('auth', False), ('rate_limit', False), (None, False)):
                account.error = error
                if not keep_data:
                    account.quotas = []
                    account.updated_at = None
                for active in ('team', 'pro'):
                    self.store.select(active)
                    card._remeasure()
                    self.assertFalse(card.grab().isNull())
                    for progress in (0.0, 0.5, 1.0):
                        island._expand = progress
                        island._target_open = progress > 0
                        island._apply_geometry()
                        self.assertFalse(island.grab().isNull())


if __name__ == '__main__':
    unittest.main()
