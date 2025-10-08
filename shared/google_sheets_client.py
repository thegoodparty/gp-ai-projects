import os
import pickle
from typing import Optional, List, Dict, Any
from pathlib import Path
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from dotenv import load_dotenv

from shared.logger import get_logger

load_dotenv()

SCOPES = ['https://www.googleapis.com/auth/spreadsheets.readonly']

class GoogleSheetsClient:
    """
    Shared client for Google Sheets API with authentication, caching, and logging.

    Features:
    - OAuth2 authentication with token caching
    - Read data from spreadsheets and specific sheets
    - Automatic token refresh
    - Error handling and logging
    """

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        token_path: Optional[str] = None,
        scopes: Optional[List[str]] = None
    ):
        """
        Initialize Google Sheets client.

        Args:
            client_id: Google OAuth client ID (defaults to DDHQ_MATCHER_GOOGLE_CLIENT_ID env var)
            client_secret: Google OAuth client secret (defaults to DDHQ_MATCHER_GOOGLE_CLIENT_SECRET env var)
            token_path: Path to store/load authentication token (defaults to ./token.pickle)
            scopes: List of OAuth scopes (defaults to readonly spreadsheets)
        """
        self.logger = get_logger(__name__)

        self.client_id = client_id or os.getenv('DDHQ_MATCHER_GOOGLE_CLIENT_ID')
        self.client_secret = client_secret or os.getenv('DDHQ_MATCHER_GOOGLE_CLIENT_SECRET')

        if not self.client_id or not self.client_secret:
            raise ValueError(
                "Missing Google OAuth credentials. Set DDHQ_MATCHER_GOOGLE_CLIENT_ID and "
                "DDHQ_MATCHER_GOOGLE_CLIENT_SECRET environment variables."
            )

        self.token_path = token_path or 'token.pickle'
        self.scopes = scopes or SCOPES

        self.creds: Optional[Credentials] = None
        self.service = None

        self.logger.info("GoogleSheetsClient initialized")

    def authenticate(self):
        """
        Authenticate with Google Sheets API using OAuth2.
        Loads cached credentials if available, otherwise initiates OAuth flow.
        """
        self.logger.info("🔐 Authenticating with Google Sheets API...")

        token_path = Path(self.token_path)

        if token_path.exists():
            with open(token_path, 'rb') as token:
                self.creds = pickle.load(token)
                self.logger.debug("Loaded cached credentials from token file")

        if not self.creds or not self.creds.valid:
            if self.creds and self.creds.expired and self.creds.refresh_token:
                self.logger.info("Refreshing expired credentials...")
                self.creds.refresh(Request())
                self.logger.info("Credentials refreshed successfully")
            else:
                self.logger.info("Starting OAuth2 flow...")
                client_config = {
                    "installed": {
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "redirect_uris": ["http://localhost"]
                    }
                }

                flow = InstalledAppFlow.from_client_config(client_config, self.scopes)
                self.creds = flow.run_local_server(port=0)
                self.logger.info("OAuth2 flow completed successfully")

            with open(token_path, 'wb') as token:
                pickle.dump(self.creds, token)
                self.logger.debug(f"Saved credentials to {token_path}")

        self.service = build('sheets', 'v4', credentials=self.creds)
        self.logger.info("✅ Authentication successful")

    def read_sheet(
        self,
        spreadsheet_id: str,
        range_name: str = 'Sheet1',
        value_render_option: str = 'UNFORMATTED_VALUE'
    ) -> List[List[Any]]:
        """
        Read data from a Google Sheet.

        Args:
            spreadsheet_id: The ID of the spreadsheet (from the URL)
            range_name: The A1 notation range or sheet name to read (e.g., 'Sheet1' or 'A1:B10')
            value_render_option: How to render values ('FORMATTED_VALUE', 'UNFORMATTED_VALUE', 'FORMULA')

        Returns:
            List of rows, where each row is a list of cell values
        """
        if not self.service:
            self.authenticate()

        self.logger.info(f"📥 Reading data from spreadsheet {spreadsheet_id}, range: {range_name}")

        try:
            sheet = self.service.spreadsheets()
            result = sheet.values().get(
                spreadsheetId=spreadsheet_id,
                range=range_name,
                valueRenderOption=value_render_option
            ).execute()

            values = result.get('values', [])

            if not values:
                self.logger.warning("No data found in sheet")
                return []

            self.logger.info(f"✅ Retrieved {len(values)} rows from Google Sheet")
            return values

        except Exception as e:
            self.logger.error(f"❌ Failed to read sheet: {str(e)}")
            raise

    def get_sheet_metadata(self, spreadsheet_id: str) -> Dict[str, Any]:
        """
        Get metadata about a spreadsheet including available sheets.

        Args:
            spreadsheet_id: The ID of the spreadsheet

        Returns:
            Dictionary with spreadsheet metadata
        """
        if not self.service:
            self.authenticate()

        self.logger.info(f"📋 Getting metadata for spreadsheet {spreadsheet_id}")

        try:
            sheet = self.service.spreadsheets()
            result = sheet.get(spreadsheetId=spreadsheet_id).execute()

            metadata = {
                'title': result.get('properties', {}).get('title', 'Unknown'),
                'sheets': []
            }

            for sheet_info in result.get('sheets', []):
                sheet_props = sheet_info.get('properties', {})
                metadata['sheets'].append({
                    'title': sheet_props.get('title', 'Unknown'),
                    'sheet_id': sheet_props.get('sheetId'),
                    'index': sheet_props.get('index'),
                    'row_count': sheet_props.get('gridProperties', {}).get('rowCount'),
                    'column_count': sheet_props.get('gridProperties', {}).get('columnCount')
                })

            self.logger.info(f"✅ Found {len(metadata['sheets'])} sheets in spreadsheet '{metadata['title']}'")
            return metadata

        except Exception as e:
            self.logger.error(f"❌ Failed to get metadata: {str(e)}")
            raise

    def list_sheets(self, spreadsheet_id: str) -> List[str]:
        """
        Get list of sheet names in a spreadsheet.

        Args:
            spreadsheet_id: The ID of the spreadsheet

        Returns:
            List of sheet names
        """
        metadata = self.get_sheet_metadata(spreadsheet_id)
        return [sheet['title'] for sheet in metadata['sheets']]

    def close(self):
        """Close the Google Sheets service connection."""
        if self.service:
            self.service.close()
            self.service = None
            self.logger.info("Google Sheets service connection closed")

    def __enter__(self) -> 'GoogleSheetsClient':
        """Context manager entry."""
        self.authenticate()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.close()
        return None


if __name__ == "__main__":
    logger = get_logger(__name__)
    logger.info("Testing Google Sheets Client")

    try:
        with GoogleSheetsClient() as client:
            logger.info("Connection successful!")

            test_spreadsheet_id = '1SnTjTOWjl-m694DZY0TA2ZplYKY_J6m-lyYhhsu_vNs'

            metadata = client.get_sheet_metadata(test_spreadsheet_id)
            logger.info(f"Spreadsheet: {metadata['title']}")
            logger.info(f"Available sheets: {[s['title'] for s in metadata['sheets']]}")

            data = client.read_sheet(test_spreadsheet_id, 'Restructured Data')
            logger.info(f"Read {len(data)} rows from 'Restructured Data' sheet")

            if data:
                logger.info(f"First row (header): {data[0]}")

    except Exception as e:
        logger.error(f"Test failed: {str(e)}")
