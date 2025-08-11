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

class PDFStorage:
    def __init__(self, base_dir: Optional[str] = None):
        if base_dir:
            self.base_dir = Path(base_dir)
        else:
            self.base_dir = Path(tempfile.gettempdir()) / "campaign_pdfs"
        
        self.base_dir.mkdir(exist_ok=True)
        logger.info(f"PDF storage initialized at {self.base_dir}")
    
    def get_session_dir(self, session_id: str) -> Path:
        session_dir = self.base_dir / session_id
        session_dir.mkdir(exist_ok=True, mode=0o700)
        return session_dir
    
    def save_pdf(self, session_id: str, pdf_data: bytes, filename: str) -> str:
        try:
            session_dir = self.get_session_dir(session_id)
            pdf_path = session_dir / filename
            
            # Write PDF data to file
            with open(pdf_path, 'wb') as f:
                f.write(pdf_data)
            
            # Set secure permissions
            pdf_path.chmod(0o600)
            
            # Load existing metadata or create new
            metadata_path = session_dir / "metadata.json"
            if metadata_path.exists():
                with open(metadata_path, 'r') as f:
                    existing_metadata = json.load(f)
                if "files" not in existing_metadata:
                    existing_metadata["files"] = []
            else:
                existing_metadata = {
                    "session_id": session_id,
                    "created_at": datetime.now().isoformat(),
                    "files": []
                }
            
            # Add this file's metadata
            file_metadata = {
                "filename": filename,
                "created_at": datetime.now().isoformat(),
                "size_bytes": len(pdf_data),
                "file_path": str(pdf_path)
            }
            existing_metadata["files"].append(file_metadata)
            
            # Save updated metadata
            with open(metadata_path, 'w') as f:
                json.dump(existing_metadata, f, indent=2)
            
            metadata_path.chmod(0o600)
            
            logger.info(f"PDF saved for session {session_id}: {filename} ({len(pdf_data)} bytes)")
            return str(pdf_path)
            
        except Exception as e:
            logger.error(f"Failed to save PDF for session {session_id}: {str(e)}")
            raise
    
    def get_pdf_path(self, session_id: str) -> Optional[Path]:
        try:
            session_dir = self.base_dir / session_id
            if not session_dir.exists():
                return None
            
            # Look for PDF files in the session directory
            pdf_files = list(session_dir.glob("*.pdf"))
            if not pdf_files:
                return None
            
            return pdf_files[0]  # Return first PDF found
            
        except Exception as e:
            logger.error(f"Failed to get PDF path for session {session_id}: {str(e)}")
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
            logger.error(f"Failed to get metadata for session {session_id}: {str(e)}")
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
                            logger.info(f"Cleaned up old session: {session_dir.name}")
                            cleaned_count += 1
                    else:
                        # No metadata, check directory modification time
                        dir_mtime = datetime.fromtimestamp(session_dir.stat().st_mtime)
                        if dir_mtime < cutoff_time:
                            shutil.rmtree(session_dir)
                            logger.info(f"Cleaned up old session (no metadata): {session_dir.name}")
                            cleaned_count += 1
                            
                except Exception as e:
                    logger.error(f"Error cleaning up session {session_dir.name}: {str(e)}")
                    continue
            
            logger.info(f"Cleanup completed: removed {cleaned_count} old sessions")
            return cleaned_count
            
        except Exception as e:
            logger.error(f"Failed to cleanup old files: {str(e)}")
            return 0
    
    def delete_session(self, session_id: str):
        try:
            session_dir = self.base_dir / session_id
            if session_dir.exists():
                shutil.rmtree(session_dir)
                logger.info(f"Deleted session: {session_id}")
            
        except Exception as e:
            logger.error(f"Failed to delete session {session_id}: {str(e)}")
    
    async def start_cleanup_task(self, cleanup_interval_hours: int = 1, max_age_hours: int = 24):
        while True:
            try:
                await asyncio.sleep(cleanup_interval_hours * 3600)
                self.cleanup_old_files(max_age_hours)
            except Exception as e:
                logger.error(f"Error in cleanup task: {str(e)}")