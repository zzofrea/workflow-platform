"""Tests for the DAG execution engine.

Covers model validation, topological sort, day-of-week filtering,
conditional gates, parallel execution, and output archival.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from workflow_platform.dag import (
    DAGConfig,
    Stage,
    StageResult,
    archive_exec_output,
    execute_dag,
    execute_stage,
    filter_stages,
    load_dag,
    resolve_tiers,
)

# -- Model Validation --


class TestDAGValidation:
    def test_valid_dag_loads(self) -> None:
        dag = DAGConfig(
            service="test",
            schedule="0 10 * * *",
            stages=[
                Stage(name="exec", type="docker-exec", container="ctr", command="echo hi"),
                Stage(name="audit", type="agent", role="auditor", depends_on=["exec"]),
            ],
        )
        assert len(dag.stages) == 2

    def test_missing_depends_on_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown stage 'nonexistent'"):
            DAGConfig(
                service="test",
                schedule="0 10 * * *",
                stages=[
                    Stage(
                        name="audit",
                        type="agent",
                        role="auditor",
                        depends_on=["nonexistent"],
                    ),
                ],
            )

    def test_circular_dependency_rejected(self) -> None:
        with pytest.raises(ValueError, match="Circular dependency"):
            DAGConfig(
                service="test",
                schedule="0 10 * * *",
                stages=[
                    Stage(
                        name="a",
                        type="agent",
                        role="r1",
                        depends_on=["b"],
                    ),
                    Stage(
                        name="b",
                        type="agent",
                        role="r2",
                        depends_on=["a"],
                    ),
                ],
            )

    def test_bad_condition_reference_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown stage 'ghost'"):
            DAGConfig(
                service="test",
                schedule="0 10 * * *",
                stages=[
                    Stage(name="exec", type="docker-exec", container="c", command="echo"),
                    Stage(
                        name="audit",
                        type="agent",
                        role="auditor",
                        depends_on=["exec"],
                        condition="ghost.success",
                    ),
                ],
            )

    def test_bad_condition_suffix_rejected(self) -> None:
        with pytest.raises(ValueError, match="must end with '.success'"):
            DAGConfig(
                service="test",
                schedule="0 10 * * *",
                stages=[
                    Stage(name="exec", type="docker-exec", container="c", command="echo"),
                    Stage(
                        name="audit",
                        type="agent",
                        role="auditor",
                        depends_on=["exec"],
                        condition="exec.failure",
                    ),
                ],
            )

    def test_duplicate_stage_names_rejected(self) -> None:
        with pytest.raises(ValueError, match="Duplicate stage name"):
            DAGConfig(
                service="test",
                schedule="0 10 * * *",
                stages=[
                    Stage(name="exec", type="docker-exec", container="c", command="echo"),
                    Stage(name="exec", type="docker-exec", container="c2", command="echo2"),
                ],
            )

    def test_invalid_stage_type_rejected(self) -> None:
        with pytest.raises(ValueError, match="must be 'docker-exec' or 'agent'"):
            Stage(name="bad", type="shell")

    def test_docker_exec_requires_container(self) -> None:
        with pytest.raises(ValueError, match="requires 'container'"):
            Stage(name="exec", type="docker-exec", command="echo hi")

    def test_docker_exec_requires_command(self) -> None:
        with pytest.raises(ValueError, match="requires 'command'"):
            Stage(name="exec", type="docker-exec", container="ctr")

    def test_agent_requires_role(self) -> None:
        with pytest.raises(ValueError, match="requires 'role'"):
            Stage(name="audit", type="agent")

    def test_invalid_when_day_rejected(self) -> None:
        with pytest.raises(ValueError, match="Invalid day abbreviation"):
            Stage(name="s", type="agent", role="r", when=["monday"])


# -- DAG Loading --


class TestLoadDAG:
    def test_loads_real_etl_dag(self) -> None:
        dag = load_dag("defendershield-etl")
        assert dag.service == "defendershield-etl"
        assert len(dag.stages) == 3
        assert dag.stages[0].name == "etl-pipeline"

    def test_missing_dag_raises(self) -> None:
        with pytest.raises(FileNotFoundError, match="DAG config not found"):
            load_dag("nonexistent-service")


# -- Day-of-Week Filtering --


class TestFilterStages:
    def test_monday_includes_mon_stages(self) -> None:
        stages = [
            Stage(name="daily", type="agent", role="auditor"),
            Stage(name="weekly", type="agent", role="analyst", when=["mon"]),
        ]
        monday = datetime(2026, 3, 9, 12, 0, tzinfo=UTC)  # Monday
        active, filtered = filter_stages(stages, monday)
        assert len(active) == 2
        assert filtered == []

    def test_tuesday_excludes_mon_stages(self) -> None:
        stages = [
            Stage(name="daily", type="agent", role="auditor"),
            Stage(name="weekly", type="agent", role="analyst", when=["mon"]),
        ]
        tuesday = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)  # Tuesday
        active, filtered = filter_stages(stages, tuesday)
        assert len(active) == 1
        assert active[0].name == "daily"
        assert filtered == ["weekly"]

    def test_all_filtered_returns_empty(self) -> None:
        stages = [
            Stage(name="sat-only", type="agent", role="r", when=["sat"]),
        ]
        wednesday = datetime(2026, 3, 11, 12, 0, tzinfo=UTC)  # Wednesday
        active, filtered = filter_stages(stages, wednesday)
        assert active == []
        assert filtered == ["sat-only"]


# -- Topological Sort --


class TestResolveTiers:
    def test_linear_chain(self) -> None:
        stages = [
            Stage(name="a", type="agent", role="r"),
            Stage(name="b", type="agent", role="r", depends_on=["a"]),
            Stage(name="c", type="agent", role="r", depends_on=["b"]),
        ]
        tiers = resolve_tiers(stages)
        assert len(tiers) == 3
        assert tiers[0][0].name == "a"
        assert tiers[1][0].name == "b"
        assert tiers[2][0].name == "c"

    def test_parallel_fan_out(self) -> None:
        stages = [
            Stage(name="root", type="agent", role="r"),
            Stage(name="left", type="agent", role="r", depends_on=["root"]),
            Stage(name="right", type="agent", role="r", depends_on=["root"]),
        ]
        tiers = resolve_tiers(stages)
        assert len(tiers) == 2
        tier_1_names = {s.name for s in tiers[1]}
        assert tier_1_names == {"left", "right"}

    def test_empty_stages(self) -> None:
        assert resolve_tiers([]) == []

    def test_single_stage(self) -> None:
        stages = [Stage(name="only", type="agent", role="r")]
        tiers = resolve_tiers(stages)
        assert len(tiers) == 1
        assert tiers[0][0].name == "only"


# -- Stage Execution --


class TestExecuteStage:
    def test_docker_exec_success(self) -> None:
        stage = Stage(name="exec", type="docker-exec", container="ctr", command="echo hi")
        mock_check = MagicMock(return_value=True)
        mock_exec = MagicMock(return_value=(0, "output\n", ""))
        mock_push = MagicMock()

        result = execute_stage(
            stage,
            {},
            "test-svc",
            set(),
            check_container_fn=mock_check,
            exec_service_fn=mock_exec,
            push_metrics_fn=mock_push,
        )

        assert result == StageResult.PASS
        mock_check.assert_called_once_with("ctr")
        mock_exec.assert_called_once()
        mock_push.assert_called_once()

    def test_docker_exec_failure(self) -> None:
        stage = Stage(name="exec", type="docker-exec", container="ctr", command="exit 1")
        mock_check = MagicMock(return_value=True)
        mock_exec = MagicMock(return_value=(1, "", "error\n"))
        mock_push = MagicMock()

        result = execute_stage(
            stage,
            {},
            "test-svc",
            set(),
            check_container_fn=mock_check,
            exec_service_fn=mock_exec,
            push_metrics_fn=mock_push,
        )

        assert result == StageResult.FAIL

    def test_container_not_running_returns_error(self) -> None:
        stage = Stage(name="exec", type="docker-exec", container="ctr", command="echo")
        mock_check = MagicMock(return_value=False)
        mock_push = MagicMock()

        result = execute_stage(
            stage,
            {},
            "test-svc",
            set(),
            check_container_fn=mock_check,
            push_metrics_fn=mock_push,
        )

        assert result == StageResult.ERROR

    def test_agent_pass(self) -> None:
        stage = Stage(name="audit", type="agent", role="auditor")
        mock_agent = MagicMock(return_value={"overall": "pass"})
        mock_push = MagicMock()

        result = execute_stage(
            stage,
            {},
            "test-svc",
            set(),
            run_agent_fn=mock_agent,
            push_metrics_fn=mock_push,
        )

        assert result == StageResult.PASS
        mock_agent.assert_called_once_with("test-svc", "auditor", max_turns=50, timeout=600)

    def test_agent_complete_is_success(self) -> None:
        stage = Stage(name="analyst", type="agent", role="analyst")
        mock_agent = MagicMock(return_value={"overall": "complete"})
        mock_push = MagicMock()

        result = execute_stage(
            stage,
            {},
            "test-svc",
            set(),
            run_agent_fn=mock_agent,
            push_metrics_fn=mock_push,
        )

        assert result == StageResult.PASS

    def test_agent_fail(self) -> None:
        stage = Stage(name="audit", type="agent", role="auditor")
        mock_agent = MagicMock(return_value={"overall": "fail"})
        mock_push = MagicMock()

        result = execute_stage(
            stage,
            {},
            "test-svc",
            set(),
            run_agent_fn=mock_agent,
            push_metrics_fn=mock_push,
        )

        assert result == StageResult.FAIL

    def test_condition_not_met_skips(self) -> None:
        stage = Stage(
            name="analyst",
            type="agent",
            role="analyst",
            condition="exec.success",
        )
        results = {"exec": StageResult.FAIL}
        mock_push = MagicMock()

        result = execute_stage(
            stage,
            results,
            "test-svc",
            set(),
            push_metrics_fn=mock_push,
        )

        assert result == StageResult.SKIPPED

    def test_condition_met_runs(self) -> None:
        stage = Stage(
            name="analyst",
            type="agent",
            role="analyst",
            condition="exec.success",
        )
        results = {"exec": StageResult.PASS}
        mock_agent = MagicMock(return_value={"overall": "complete"})
        mock_push = MagicMock()

        result = execute_stage(
            stage,
            results,
            "test-svc",
            set(),
            run_agent_fn=mock_agent,
            push_metrics_fn=mock_push,
        )

        assert result == StageResult.PASS

    def test_condition_on_skipped_stage_skips(self) -> None:
        """Scenario 7: condition on a when-filtered stage => skipped."""
        stage = Stage(
            name="analyst",
            type="agent",
            role="analyst",
            condition="exec.success",
        )
        mock_push = MagicMock()

        result = execute_stage(
            stage,
            {},
            "test-svc",
            {"exec"},  # exec was filtered out
            push_metrics_fn=mock_push,
        )

        assert result == StageResult.SKIPPED

    def test_metrics_pushed_per_stage(self) -> None:
        stage = Stage(name="audit", type="agent", role="auditor")
        mock_agent = MagicMock(return_value={"overall": "pass"})
        mock_push = MagicMock()

        execute_stage(
            stage,
            {},
            "test-svc",
            set(),
            run_agent_fn=mock_agent,
            push_metrics_fn=mock_push,
        )

        mock_push.assert_called_once()
        call_args = mock_push.call_args
        assert call_args[0][0] == "test-svc"  # service
        assert call_args[0][1].name == "audit"  # stage
        assert call_args[0][2] == StageResult.PASS  # result


# -- Full DAG Execution --


class TestExecuteDAG:
    def _make_dag(self, stages: list[Stage]) -> DAGConfig:
        return DAGConfig(service="test-svc", schedule="0 10 * * *", stages=stages)

    def test_happy_path_linear(self) -> None:
        """All stages pass in sequence."""
        dag = self._make_dag(
            [
                Stage(name="exec", type="docker-exec", container="c", command="echo"),
                Stage(name="audit", type="agent", role="auditor", depends_on=["exec"]),
            ]
        )
        mock_exec = MagicMock(return_value=(0, "ok", ""))
        mock_check = MagicMock(return_value=True)
        mock_agent = MagicMock(return_value={"overall": "pass"})
        mock_push = MagicMock()

        results = execute_dag(
            dag,
            exec_service_fn=mock_exec,
            check_container_fn=mock_check,
            run_agent_fn=mock_agent,
            push_metrics_fn=mock_push,
        )

        assert results["exec"] == StageResult.PASS
        assert results["audit"] == StageResult.PASS

    def test_exec_failure_gates_conditional(self) -> None:
        """Scenario 4: exec fails, auditor still runs, analyst skipped by condition."""
        dag = self._make_dag(
            [
                Stage(name="exec", type="docker-exec", container="c", command="echo"),
                Stage(name="auditor", type="agent", role="auditor", depends_on=["exec"]),
                Stage(
                    name="analyst",
                    type="agent",
                    role="analyst",
                    depends_on=["auditor"],
                    condition="exec.success",
                ),
            ]
        )
        mock_exec = MagicMock(return_value=(1, "", "error"))
        mock_check = MagicMock(return_value=True)
        mock_agent = MagicMock(return_value={"overall": "pass"})
        mock_push = MagicMock()

        results = execute_dag(
            dag,
            exec_service_fn=mock_exec,
            check_container_fn=mock_check,
            run_agent_fn=mock_agent,
            push_metrics_fn=mock_push,
        )

        assert results["exec"] == StageResult.FAIL
        assert results["auditor"] == StageResult.PASS
        assert results["analyst"] == StageResult.SKIPPED

    def test_skipped_dependency_treated_as_satisfied(self) -> None:
        """Scenario 6: when-filtered dep doesn't block downstream."""
        dag = self._make_dag(
            [
                Stage(name="exec", type="docker-exec", container="c", command="echo", when=["mon"]),
                Stage(name="auditor", type="agent", role="auditor", depends_on=["exec"]),
            ]
        )
        # Tuesday -- exec gets filtered out
        tuesday = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
        mock_agent = MagicMock(return_value={"overall": "pass"})
        mock_push = MagicMock()

        results = execute_dag(
            dag,
            utc_now=tuesday,
            run_agent_fn=mock_agent,
            push_metrics_fn=mock_push,
        )

        assert "exec" not in results  # filtered out
        assert results["auditor"] == StageResult.PASS

    def test_condition_on_skipped_stage_also_skips(self) -> None:
        """Scenario 7: condition on when-filtered stage => skipped."""
        dag = self._make_dag(
            [
                Stage(name="exec", type="docker-exec", container="c", command="echo", when=["mon"]),
                Stage(
                    name="analyst",
                    type="agent",
                    role="analyst",
                    depends_on=["exec"],
                    condition="exec.success",
                ),
            ]
        )
        tuesday = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)
        mock_push = MagicMock()

        results = execute_dag(
            dag,
            utc_now=tuesday,
            push_metrics_fn=mock_push,
        )

        assert results["analyst"] == StageResult.SKIPPED

    def test_all_stages_filtered_exits_clean(self) -> None:
        """Scenario 9: every stage filtered out => empty results, exit 0."""
        dag = self._make_dag(
            [
                Stage(name="sat-only", type="agent", role="r", when=["sat"]),
            ]
        )
        wednesday = datetime(2026, 3, 11, 12, 0, tzinfo=UTC)
        mock_push = MagicMock()

        results = execute_dag(dag, utc_now=wednesday, push_metrics_fn=mock_push)

        assert results == {}

    def test_parallel_fan_out(self) -> None:
        """Scenario 3: two stages with same dependency run in same tier."""
        dag = self._make_dag(
            [
                Stage(name="exec", type="docker-exec", container="c", command="echo"),
                Stage(name="audit", type="agent", role="auditor", depends_on=["exec"]),
                Stage(name="report", type="agent", role="reporter", depends_on=["exec"]),
            ]
        )
        mock_exec = MagicMock(return_value=(0, "ok", ""))
        mock_check = MagicMock(return_value=True)
        mock_agent = MagicMock(return_value={"overall": "pass"})
        mock_push = MagicMock()

        results = execute_dag(
            dag,
            exec_service_fn=mock_exec,
            check_container_fn=mock_check,
            run_agent_fn=mock_agent,
            push_metrics_fn=mock_push,
        )

        assert results["exec"] == StageResult.PASS
        assert results["audit"] == StageResult.PASS
        assert results["report"] == StageResult.PASS


# -- Output Archival --


class TestArchiveExecOutput:
    def test_creates_output_file(self, tmp_path: Path) -> None:
        with patch("workflow_platform.dag.Path.home", return_value=tmp_path):
            result = archive_exec_output("test-svc", "etl-pipeline", "ok\n", "", 0)

        assert result is not None
        assert result.exists()
        content = result.read_text()
        assert "etl-pipeline" in content
        assert "EXIT CODE: 0" in content
        assert "ok\n" in content

    def test_archive_path_structure(self, tmp_path: Path) -> None:
        with patch("workflow_platform.dag.Path.home", return_value=tmp_path):
            result = archive_exec_output("my-svc", "my-stage", "", "", 1)

        assert result is not None
        assert "agent-output" in str(result)
        assert "my-svc" in str(result)
        assert "exec_my-stage_" in str(result)


# -- Metrics Extension --


class TestMetricsStageLabel:
    def test_push_metrics_with_stage_label(self) -> None:
        with patch("workflow_platform.metrics.push_to_gateway") as mock_push:
            from workflow_platform.metrics import push_metrics

            report = {
                "overall": "pass",
                "duration_seconds": 10,
                "scenarios_pass": 1,
                "scenarios_fail": 0,
            }
            push_metrics("test-svc", "auditor", report, stage="etl-pipeline")

            mock_push.assert_called_once()
            call_kwargs = mock_push.call_args[1]
            assert "etl-pipeline" in call_kwargs["job"]

    def test_push_metrics_without_stage_backwards_compatible(self) -> None:
        with patch("workflow_platform.metrics.push_to_gateway") as mock_push:
            from workflow_platform.metrics import push_metrics

            report = {
                "overall": "pass",
                "duration_seconds": 10,
                "scenarios_pass": 1,
                "scenarios_fail": 0,
            }
            push_metrics("test-svc", "auditor", report)

            mock_push.assert_called_once()
            call_kwargs = mock_push.call_args[1]
            assert call_kwargs["job"] == "workflow_agent_test-svc_auditor"
