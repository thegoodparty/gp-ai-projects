"""Pins the capability-prompt contract: the single source of repo guidance.

clickup_bot's instruction templates deliberately do NOT carry repo guidance;
if it disappears from build_capability_prompt(), agents clone archived repos
and burn whole runs (July 2026 incident class).
"""

from engineer_agent.agent.config import build_capability_prompt


def test_capability_prompt_points_at_omni_monorepo():
    prompt = build_capability_prompt()
    assert "thegoodparty/omni" in prompt
    assert "packages/" in prompt
    assert "gp-webapp" in prompt


def test_capability_prompt_warns_off_archived_repos():
    prompt = build_capability_prompt()
    assert "archived" in prompt
    assert "never clone them" in prompt


def test_capability_prompt_keeps_live_non_omni_repos():
    prompt = build_capability_prompt()
    assert "gp-ai-projects" in prompt
    assert "gp-data-platform" in prompt


def test_capability_prompt_targets_omni_develop_for_prs():
    prompt = build_capability_prompt()
    assert "`develop`" in prompt
