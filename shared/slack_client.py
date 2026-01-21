import os
import re
import asyncio
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timedelta
from dotenv import load_dotenv

from shared.logger import get_logger

load_dotenv()

try:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
except ImportError:
    raise ImportError("slack-sdk is required. Install with: uv add slack-sdk")


class SlackClient:
    def __init__(self, bot_token: Optional[str] = None):
        self.logger = get_logger(__name__)
        self.bot_token = bot_token or os.getenv("SLACK_BOT_TOKEN")

        if not self.bot_token:
            raise ValueError("Slack bot token is required. Set SLACK_BOT_TOKEN environment variable or pass bot_token parameter.")

        if self.bot_token == "xoxb-YOUR_BOT_TOKEN_HERE":
            raise ValueError("Please replace the placeholder SLACK_BOT_TOKEN with your actual bot token.")

        self.client = WebClient(token=self.bot_token)
        self._channel_cache: Dict[str, str] = {}
        self._user_cache: Dict[str, Dict[str, Any]] = {}
        self.logger.info("SlackClient initialized successfully")

    async def list_channels(self, types: str = "public_channel,private_channel") -> List[Dict[str, Any]]:
        self.logger.info("Listing Slack channels")

        try:
            channels = []
            cursor = None

            while True:
                result = await asyncio.to_thread(
                    self.client.conversations_list,
                    types=types,
                    cursor=cursor,
                    limit=200
                )

                channels.extend(result.get("channels", []))
                cursor = result.get("response_metadata", {}).get("next_cursor")

                if not cursor:
                    break

            for channel in channels:
                self._channel_cache[channel["name"]] = channel["id"]
                self._channel_cache[channel["id"]] = channel["id"]

            self.logger.info(f"Found {len(channels)} channels")
            return channels

        except SlackApiError as e:
            self.logger.error(f"Failed to list channels: {e.response['error']}")
            raise

    async def get_channel_id(self, channel_name_or_id: str) -> str:
        if channel_name_or_id.startswith("C"):
            return channel_name_or_id

        if channel_name_or_id in self._channel_cache:
            return self._channel_cache[channel_name_or_id]

        await self.list_channels()

        clean_name = channel_name_or_id.lstrip("#")
        if clean_name in self._channel_cache:
            return self._channel_cache[clean_name]

        raise ValueError(f"Channel '{channel_name_or_id}' not found")

    async def get_user_info(self, user_id: str) -> Dict[str, Any]:
        if user_id in self._user_cache:
            return self._user_cache[user_id]

        try:
            result = await asyncio.to_thread(
                self.client.users_info,
                user=user_id
            )
            user_info = result.get("user", {})
            self._user_cache[user_id] = user_info
            return user_info
        except SlackApiError as e:
            self.logger.warning(f"Failed to get user info for {user_id}: {e.response['error']}")
            return {"id": user_id, "name": user_id}

    async def get_channel_history(
        self,
        channel: str,
        limit: int = 100,
        oldest: Optional[datetime] = None,
        latest: Optional[datetime] = None,
        include_replies: bool = False,
        resolve_users: bool = True
    ) -> List[Dict[str, Any]]:
        channel_id = await self.get_channel_id(channel)
        self.logger.info(f"Fetching history for channel {channel} (ID: {channel_id})")

        try:
            params = {
                "channel": channel_id,
                "limit": min(limit, 1000)
            }

            if oldest:
                params["oldest"] = str(oldest.timestamp())
            if latest:
                params["latest"] = str(latest.timestamp())

            messages = []
            cursor = None

            while len(messages) < limit:
                if cursor:
                    params["cursor"] = cursor

                result = await asyncio.to_thread(
                    self.client.conversations_history,
                    **params
                )

                batch = result.get("messages", [])
                messages.extend(batch)

                cursor = result.get("response_metadata", {}).get("next_cursor")
                if not cursor or not batch:
                    break

            messages = messages[:limit]

            if include_replies:
                messages = await self._fetch_replies(channel_id, messages)

            if resolve_users:
                messages = await self._resolve_users_in_messages(messages)

            self.logger.info(f"Fetched {len(messages)} messages from channel {channel}")
            return messages

        except SlackApiError as e:
            self.logger.error(f"Failed to fetch channel history: {e.response['error']}")
            raise

    async def _fetch_replies(self, channel_id: str, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        for message in messages:
            if message.get("reply_count", 0) > 0:
                try:
                    result = await asyncio.to_thread(
                        self.client.conversations_replies,
                        channel=channel_id,
                        ts=message["ts"]
                    )
                    replies = result.get("messages", [])[1:]
                    message["replies"] = replies
                except SlackApiError as e:
                    self.logger.warning(f"Failed to fetch replies: {e.response['error']}")

        return messages

    async def _resolve_users_in_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        user_ids = set()
        for msg in messages:
            if "user" in msg:
                user_ids.add(msg["user"])
            for reply in msg.get("replies", []):
                if "user" in reply:
                    user_ids.add(reply["user"])

        for user_id in user_ids:
            if user_id not in self._user_cache:
                await self.get_user_info(user_id)

        for msg in messages:
            if "user" in msg and msg["user"] in self._user_cache:
                user = self._user_cache[msg["user"]]
                msg["user_name"] = user.get("real_name") or user.get("name", msg["user"])
            for reply in msg.get("replies", []):
                if "user" in reply and reply["user"] in self._user_cache:
                    user = self._user_cache[reply["user"]]
                    reply["user_name"] = user.get("real_name") or user.get("name", reply["user"])

        return messages

    async def get_messages_since(
        self,
        channel: str,
        days: int = 7,
        include_replies: bool = False,
        resolve_users: bool = True
    ) -> List[Dict[str, Any]]:
        oldest = datetime.now() - timedelta(days=days)
        return await self.get_channel_history(
            channel=channel,
            limit=1000,
            oldest=oldest,
            include_replies=include_replies,
            resolve_users=resolve_users
        )

    @staticmethod
    def parse_slack_url(url: str) -> Tuple[str, str]:
        match = re.search(r'/archives/([A-Z0-9]+)/p(\d+)', url)
        if not match:
            raise ValueError(f"Invalid Slack URL format: {url}")

        channel_id = match.group(1)
        ts_raw = match.group(2)
        thread_ts = f"{ts_raw[:10]}.{ts_raw[10:]}"

        return channel_id, thread_ts

    async def get_thread(
        self,
        channel: str,
        thread_ts: str,
        resolve_users: bool = True
    ) -> List[Dict[str, Any]]:
        channel_id = await self.get_channel_id(channel)
        self.logger.info(f"Fetching thread {thread_ts} from channel {channel_id}")

        try:
            result = await asyncio.to_thread(
                self.client.conversations_replies,
                channel=channel_id,
                ts=thread_ts
            )
            messages = result.get("messages", [])

            if resolve_users:
                messages = await self._resolve_users_in_messages(messages)

            self.logger.info(f"Fetched {len(messages)} messages in thread")
            return messages

        except SlackApiError as e:
            self.logger.error(f"Failed to fetch thread: {e.response['error']}")
            raise

    async def get_thread_from_url(self, url: str, resolve_users: bool = True) -> List[Dict[str, Any]]:
        channel_id, thread_ts = self.parse_slack_url(url)
        return await self.get_thread(channel_id, thread_ts, resolve_users=resolve_users)

    def format_thread(self, messages: List[Dict[str, Any]], include_timestamps: bool = True) -> str:
        formatted = []

        for i, msg in enumerate(messages):
            ts = datetime.fromtimestamp(float(msg.get("ts", 0)))
            user = msg.get("user_name", msg.get("user", "Unknown"))
            text = msg.get("text", "")

            prefix = "" if i == 0 else "  └─ "
            if include_timestamps:
                formatted.append(f"{prefix}[{ts.strftime('%Y-%m-%d %H:%M')}] {user}: {text}")
            else:
                formatted.append(f"{prefix}{user}: {text}")

        return "\n".join(formatted)

    def format_messages(self, messages: List[Dict[str, Any]], include_timestamps: bool = True) -> str:
        formatted = []

        for msg in sorted(messages, key=lambda x: float(x.get("ts", 0))):
            ts = datetime.fromtimestamp(float(msg.get("ts", 0)))
            user = msg.get("user_name", msg.get("user", "Unknown"))
            text = msg.get("text", "")

            if include_timestamps:
                formatted.append(f"[{ts.strftime('%Y-%m-%d %H:%M')}] {user}: {text}")
            else:
                formatted.append(f"{user}: {text}")

            for reply in msg.get("replies", []):
                reply_ts = datetime.fromtimestamp(float(reply.get("ts", 0)))
                reply_user = reply.get("user_name", reply.get("user", "Unknown"))
                reply_text = reply.get("text", "")

                if include_timestamps:
                    formatted.append(f"  └─ [{reply_ts.strftime('%Y-%m-%d %H:%M')}] {reply_user}: {reply_text}")
                else:
                    formatted.append(f"  └─ {reply_user}: {reply_text}")

        return "\n".join(formatted)


if __name__ == "__main__":
    async def main():
        client = SlackClient()

        print("Listing channels...")
        channels = await client.list_channels()
        for ch in channels[:10]:
            print(f"  #{ch['name']} ({ch['id']})")

        print("\nFetching recent messages from #bugs...")
        try:
            messages = await client.get_messages_since("bugs", days=7, include_replies=True)
            print(client.format_messages(messages[:10]))
        except ValueError as e:
            print(f"Error: {e}")

    asyncio.run(main())
