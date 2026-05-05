from unittest.mock import patch

from shared.aws_clients import get_client, reset_client_cache


class TestAwsClientMemoization:
    def setup_method(self):
        reset_client_cache()

    def teardown_method(self):
        reset_client_cache()

    def test_first_call_creates_boto3_client(self):
        with patch("shared.aws_clients.boto3") as mock_boto3:
            get_client("s3")
            mock_boto3.client.assert_called_once_with("s3")

    def test_second_call_returns_cached(self):
        with patch("shared.aws_clients.boto3") as mock_boto3:
            first = get_client("s3")
            second = get_client("s3")
            assert first is second
            mock_boto3.client.assert_called_once_with("s3")

    def test_different_services_create_separate_clients(self):
        with patch("shared.aws_clients.boto3") as mock_boto3:
            get_client("s3")
            get_client("sqs")
            assert mock_boto3.client.call_count == 2
            services = {call.args[0] for call in mock_boto3.client.call_args_list}
            assert services == {"s3", "sqs"}

    def test_reset_clears_cache(self):
        with patch("shared.aws_clients.boto3") as mock_boto3:
            get_client("s3")
            reset_client_cache()
            get_client("s3")
            assert mock_boto3.client.call_count == 2
