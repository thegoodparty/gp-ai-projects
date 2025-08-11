import os
import json
import asyncio
import tempfile
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import shutil

from shared.logger import get_logger

logger = get_logger(__name__)

class JSONStorage:
    def __init__(self, base_dir: Optional[str] = None):
        if base_dir:
            self.base_dir = Path(base_dir)
        else:
            self.base_dir = Path(tempfile.gettempdir()) / "campaign_json"
        
        self.base_dir.mkdir(exist_ok=True)
        logger.info(f"JSON storage initialized at {self.base_dir}")
    
    def get_session_dir(self, session_id: str) -> Path:
        session_dir = self.base_dir / session_id
        session_dir.mkdir(exist_ok=True, mode=0o700)
        return session_dir
    
    def save_json(self, session_id: str, json_data: Dict[str, Any], filename: str) -> str:
        try:
            session_dir = self.get_session_dir(session_id)
            json_path = session_dir / filename
            
            # Write JSON data to file with pretty formatting
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(json_data, f, indent=2, ensure_ascii=False)
            
            # Set secure permissions
            json_path.chmod(0o600)
            
            # Save metadata
            metadata = {
                "filename": filename,
                "created_at": datetime.now().isoformat(),
                "size_bytes": json_path.stat().st_size,
                "session_id": session_id,
                "file_type": "json"
            }
            
            metadata_path = session_dir / "metadata.json"
            with open(metadata_path, 'w') as f:
                json.dump(metadata, f, indent=2)
            
            metadata_path.chmod(0o600)
            
            logger.info(f"JSON saved for session {session_id}: {filename} ({metadata['size_bytes']} bytes)")
            return str(json_path)
            
        except Exception as e:
            logger.error(f"Failed to save JSON for session {session_id}: {str(e)}")
            raise
    
    def get_json_path(self, session_id: str) -> Optional[Path]:
        try:
            session_dir = self.base_dir / session_id
            if not session_dir.exists():
                return None
            
            # Look for JSON files in the session directory
            json_files = list(session_dir.glob("*.json"))
            # Filter out metadata.json
            json_files = [f for f in json_files if f.name != "metadata.json"]
            
            if not json_files:
                return None
            
            return json_files[0]  # Return first JSON data file found
            
        except Exception as e:
            logger.error(f"Failed to get JSON path for session {session_id}: {str(e)}")
            return None
    
    def load_json(self, session_id: str) -> Optional[Dict[str, Any]]:
        try:
            json_path = self.get_json_path(session_id)
            if not json_path or not json_path.exists():
                return None
            
            with open(json_path, 'r', encoding='utf-8') as f:
                return json.load(f)
                
        except Exception as e:
            logger.error(f"Failed to load JSON for session {session_id}: {str(e)}")
            return None
    
    def get_metadata(self, session_id: str) -> Optional[Dict[str, Any]]:
        try:
            session_dir = self.base_dir / session_id
            metadata_path = session_dir / "metadata.json"
            
            if not metadata_path.exists():
                return None
            
            with open(metadata_path, 'r') as f:
                return json.load(f)
                
        except Exception as e:
            logger.error(f"Failed to get JSON metadata for session {session_id}: {str(e)}")
            return None
    
    def cleanup_old_files(self, max_age_hours: int = 24):
        try:
            cutoff_time = datetime.now() - timedelta(hours=max_age_hours)
            cleaned_count = 0
            
            for session_dir in self.base_dir.iterdir():
                if not session_dir.is_dir():
                    continue
                
                try:
                    metadata_path = session_dir / "metadata.json"
                    if metadata_path.exists():
                        with open(metadata_path, 'r') as f:
                            metadata = json.load(f)
                        
                        created_at = datetime.fromisoformat(metadata.get("created_at", ""))
                        if created_at < cutoff_time:
                            shutil.rmtree(session_dir)
                            logger.info(f"Cleaned up old JSON session: {session_dir.name}")
                            cleaned_count += 1
                    else:
                        # No metadata, check directory modification time
                        dir_mtime = datetime.fromtimestamp(session_dir.stat().st_mtime)
                        if dir_mtime < cutoff_time:
                            shutil.rmtree(session_dir)
                            logger.info(f"Cleaned up old JSON session (no metadata): {session_dir.name}")
                            cleaned_count += 1
                            
                except Exception as e:
                    logger.error(f"Error cleaning up JSON session {session_dir.name}: {str(e)}")
                    continue
            
            logger.info(f"JSON cleanup completed: removed {cleaned_count} old sessions")
            return cleaned_count
            
        except Exception as e:
            logger.error(f"Failed to cleanup old JSON files: {str(e)}")
            return 0
    
    def delete_session(self, session_id: str):
        try:
            session_dir = self.base_dir / session_id
            if session_dir.exists():
                shutil.rmtree(session_dir)
                logger.info(f"Deleted JSON session: {session_id}")
            
        except Exception as e:
            logger.error(f"Failed to delete JSON session {session_id}: {str(e)}")
    
    async def start_cleanup_task(self, cleanup_interval_hours: int = 1, max_age_hours: int = 24):
        while True:
            try:
                await asyncio.sleep(cleanup_interval_hours * 3600)
                self.cleanup_old_files(max_age_hours)
            except Exception as e:
                logger.error(f"Error in JSON cleanup task: {str(e)}")