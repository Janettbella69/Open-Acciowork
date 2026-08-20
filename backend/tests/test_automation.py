"""FR-26/FR-15 groundwork: notification inbox and standing-intent throttle budget.

PRD §4.16: standing intents carry anti-nag budgets — cooldown / max triggers /
expiry (OpenClaw-proven defaults 24h / 3 / 90d) — and heartbeat results stay
silent unless something changed.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from ophira_backend.automation import IntentStore, NotificationStore
from ophira_backend.models import StandingIntentCreate

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def build_stores(tmp_path: Path) -> tuple[NotificationStore, IntentStore]:
    return NotificationStore(tmp_path / "notifications"), IntentStore(tmp_path / "intents")


class TestNotificationInbox:
    def test_add_list_and_mark_read(self, tmp_path: Path) -> None:
        inbox, _ = build_stores(tmp_path)
        first = inbox.add(title="竞品价格变化", body="BlendGo 降价 2 EUR", severity="info",
                          project_id="project_1", run_id="run_1")
        second = inbox.add(title="定时任务失败", body="凭据无效", severity="error",
                           schedule_id="schedule_1")

        assert [item.id for item in inbox.list()] == [second.id, first.id]
        assert all(item.read is False for item in inbox.list())
        assert len(inbox.list(unread_only=True)) == 2

        marked = inbox.mark_read(first.id)
        assert marked.read is True
        assert [item.id for item in inbox.list(unread_only=True)] == [second.id]

        assert inbox.mark_all_read() == 1
        assert inbox.list(unread_only=True) == []

    def test_notifications_persist_across_store_instances(self, tmp_path: Path) -> None:
        inbox, _ = build_stores(tmp_path)
        inbox.add(title="t", body="b", severity="warning")
        reopened = NotificationStore(tmp_path / "notifications")
        assert len(reopened.list()) == 1
        assert reopened.list()[0].severity == "warning"


class TestStandingIntentBudget:
    def make_intent(self, store: IntentStore, **overrides):
        payload = {
            "project_id": "project_1",
            "intent": "竞品 BlendGo 价格变化超过 5% 时告诉我",
        }
        payload.update(overrides)
        return store.create(StandingIntentCreate(**payload), now=NOW)

    def test_prd_defaults_are_24h_3_triggers_90_days(self, tmp_path: Path) -> None:
        _, intents = build_stores(tmp_path)
        intent = self.make_intent(intents)
        assert intent.cooldown_hours == 24
        assert intent.max_triggers == 3
        assert intent.expires_at == NOW + timedelta(days=90)
        assert intent.enabled is True

    def test_trigger_then_cooldown_then_exhaustion(self, tmp_path: Path) -> None:
        _, intents = build_stores(tmp_path)
        intent = self.make_intent(intents, max_triggers=2)

        outcome, updated = intents.try_trigger(intent.id, now=NOW)
        assert outcome == "triggered"
        assert updated.triggered_count == 1

        outcome, _ = intents.try_trigger(intent.id, now=NOW + timedelta(hours=1))
        assert outcome == "cooldown"

        outcome, updated = intents.try_trigger(intent.id, now=NOW + timedelta(hours=25))
        assert outcome == "triggered"
        assert updated.triggered_count == 2

        outcome, _ = intents.try_trigger(intent.id, now=NOW + timedelta(hours=50))
        assert outcome == "exhausted"

    def test_expired_intent_is_disabled(self, tmp_path: Path) -> None:
        _, intents = build_stores(tmp_path)
        intent = self.make_intent(intents, expires_days=1)

        outcome, updated = intents.try_trigger(intent.id, now=NOW + timedelta(days=2))
        assert outcome == "expired"
        assert updated.enabled is False
        assert intents.get(intent.id).enabled is False

    def test_disabled_intent_never_triggers(self, tmp_path: Path) -> None:
        _, intents = build_stores(tmp_path)
        intent = self.make_intent(intents)
        intents.set_enabled(intent.id, False)

        outcome, _ = intents.try_trigger(intent.id, now=NOW)
        assert outcome == "disabled"

    def test_list_filters_by_project_and_enabled(self, tmp_path: Path) -> None:
        _, intents = build_stores(tmp_path)
        keep = self.make_intent(intents)
        other = self.make_intent(intents, project_id="project_2")
        intents.set_enabled(other.id, False)

        assert {item.id for item in intents.list()} == {keep.id, other.id}
        assert [item.id for item in intents.list(project_id="project_1")] == [keep.id]
        assert [item.id for item in intents.list(enabled_only=True)] == [keep.id]
