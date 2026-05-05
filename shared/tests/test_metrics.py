from unittest.mock import MagicMock, patch

from shared.metrics import emit_metric


class TestEmitMetric:
    def test_emits_single_metric_with_dimensions(self):
        mock_cw = MagicMock()
        with patch("shared.metrics.get_client", return_value=mock_cw):
            emit_metric(
                namespace="PMFEngine",
                name="RunCount",
                value=1,
                unit="Count",
                dimensions={"Environment": "dev", "ExperimentId": "voter_targeting"},
            )

        mock_cw.put_metric_data.assert_called_once()
        kwargs = mock_cw.put_metric_data.call_args.kwargs
        assert kwargs["Namespace"] == "PMFEngine"
        md = kwargs["MetricData"][0]
        assert md["MetricName"] == "RunCount"
        assert md["Value"] == 1
        assert md["Unit"] == "Count"
        dims = {d["Name"]: d["Value"] for d in md["Dimensions"]}
        assert dims == {"Environment": "dev", "ExperimentId": "voter_targeting"}

    def test_swallows_put_metric_data_exceptions(self):
        mock_cw = MagicMock()
        mock_cw.put_metric_data.side_effect = RuntimeError("CloudWatch down")
        with patch("shared.metrics.get_client", return_value=mock_cw):
            emit_metric(
                namespace="PMFEngine",
                name="Test",
                value=1,
                unit="Count",
                dimensions={},
            )

    def test_defaults_value_and_unit(self):
        mock_cw = MagicMock()
        with patch("shared.metrics.get_client", return_value=mock_cw):
            emit_metric(namespace="X", name="Y", dimensions={"k": "v"})

        md = mock_cw.put_metric_data.call_args.kwargs["MetricData"][0]
        assert md["Value"] == 1
        assert md["Unit"] == "Count"

    def test_empty_dimensions_produces_empty_list(self):
        mock_cw = MagicMock()
        with patch("shared.metrics.get_client", return_value=mock_cw):
            emit_metric(namespace="X", name="Y", dimensions={})

        md = mock_cw.put_metric_data.call_args.kwargs["MetricData"][0]
        assert md["Dimensions"] == []
