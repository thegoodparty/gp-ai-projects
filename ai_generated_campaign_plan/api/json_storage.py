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
    
    def get_json_filename(self, session_id: str) -> str:
        return f"{session_id}.json"
    
    def get_metadata_filename(self, session_id: str) -> str:
        return f"{session_id}_metadata.json"
    
    def get_json_path(self, session_id: str) -> Optional[Path]:
        json_path = self.base_dir / self.get_json_filename(session_id)
        return json_path if json_path.exists() else None
    
    def get_metadata_path(self, session_id: str) -> Path:
        return self.base_dir / self.get_metadata_filename(session_id)
    
    def save_json(self, session_id: str, json_data: Dict[str, Any], original_filename: str) -> str:
        try:
            json_path = self.base_dir / self.get_json_filename(session_id)
            
            # Write JSON data to file with pretty formatting
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(json_data, f, indent=2, ensure_ascii=False)
            
            # Set secure permissions
            json_path.chmod(0o600)
            
            # Save metadata
            metadata = {
                "session_id": session_id,
                "original_filename": original_filename,
                "created_at": datetime.now().isoformat(),
                "size_bytes": json_path.stat().st_size,
                "file_path": str(json_path),
                "file_type": "json"
            }
            
            metadata_path = self.get_metadata_path(session_id)
            with open(metadata_path, 'w') as f:
                json.dump(metadata, f, indent=2)
            
            metadata_path.chmod(0o600)
            
            logger.info(f"JSON saved for session {session_id}: {original_filename} ({metadata['size_bytes']} bytes)")
            return str(json_path)
            
        except Exception as e:
            logger.error(f"Failed to save JSON for session {session_id}: {str(e)}")
            raise
    
    
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
            metadata_path = self.get_metadata_path(session_id)
            
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
            
            # Find all metadata files
            metadata_files = list(self.base_dir.glob("*_metadata.json"))
            
            for metadata_path in metadata_files:
                try:
                    with open(metadata_path, 'r') as f:
                        metadata = json.load(f)
                    
                    created_at = datetime.fromisoformat(metadata.get("created_at", ""))
                    if created_at < cutoff_time:
                        session_id = metadata.get("session_id")
                        if session_id:
                            self.delete_session(session_id)
                            cleaned_count += 1
                        
                except Exception as e:
                    logger.error(f"Error processing JSON metadata file {metadata_path.name}: {str(e)}")
                    continue
            
            # Also clean up orphaned JSON files without metadata
            json_files = list(self.base_dir.glob("*.json"))
            for json_path in json_files:
                try:
                    # Skip metadata files
                    if json_path.name.endswith('_metadata.json'):
                        continue
                        
                    # Extract session ID from filename
                    if json_path.name.endswith('.json'):
                        session_id = json_path.name[:-5]  # Remove .json extension
                        metadata_path = self.get_metadata_path(session_id)
                        
                        if not metadata_path.exists():
                            # Orphaned JSON file, check its modification time
                            file_mtime = datetime.fromtimestamp(json_path.stat().st_mtime)
                            if file_mtime < cutoff_time:
                                json_path.unlink()
                                logger.info(f"Cleaned up orphaned JSON: {json_path.name}")
                                cleaned_count += 1
                                
                except Exception as e:
                    logger.error(f"Error cleaning up JSON file {json_path.name}: {str(e)}")
                    continue
            
            logger.info(f"JSON cleanup completed: removed {cleaned_count} old files")
            return cleaned_count
            
        except Exception as e:
            logger.error(f"Failed to cleanup old JSON files: {str(e)}")
            return 0
    
    def delete_session(self, session_id: str):
        try:
            json_path = self.base_dir / self.get_json_filename(session_id)
            metadata_path = self.get_metadata_path(session_id)
            
            files_deleted = 0
            if json_path.exists():
                json_path.unlink()
                files_deleted += 1
            if metadata_path.exists():
                metadata_path.unlink()
                files_deleted += 1
                
            if files_deleted > 0:
                logger.info(f"Deleted JSON session {session_id}: {files_deleted} files removed")
            
        except Exception as e:
            logger.error(f"Failed to delete JSON session {session_id}: {str(e)}")
    
    
    async def start_cleanup_task(self, cleanup_interval_hours: int = 1, max_age_hours: int = 24):
        while True:
            try:
                await asyncio.sleep(cleanup_interval_hours * 3600)
                self.cleanup_old_files(max_age_hours)
            except Exception as e:
                logger.error(f"Error in JSON cleanup task: {str(e)}")