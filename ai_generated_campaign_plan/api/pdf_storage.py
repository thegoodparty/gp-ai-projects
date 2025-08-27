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
    
    def get_pdf_filename(self, session_id: str) -> str:
        return f"{session_id}.pdf"
    
    def get_metadata_filename(self, session_id: str) -> str:
        return f"{session_id}_metadata.json"
    
    def get_pdf_path(self, session_id: str) -> Optional[Path]:
        pdf_path = self.base_dir / self.get_pdf_filename(session_id)
        return pdf_path if pdf_path.exists() else None
    
    def get_metadata_path(self, session_id: str) -> Path:
        return self.base_dir / self.get_metadata_filename(session_id)
    
    def save_pdf(self, session_id: str, pdf_data: bytes, original_filename: str) -> str:
        try:
            pdf_path = self.base_dir / self.get_pdf_filename(session_id)
            
            # Write PDF data to file
            with open(pdf_path, 'wb') as f:
                f.write(pdf_data)
            
            # Set secure permissions
            pdf_path.chmod(0o600)
            
            # Save metadata
            metadata = {
                "session_id": session_id,
                "original_filename": original_filename,
                "created_at": datetime.now().isoformat(),
                "size_bytes": len(pdf_data),
                "file_path": str(pdf_path),
                "file_type": "pdf"
            }
            
            metadata_path = self.get_metadata_path(session_id)
            with open(metadata_path, 'w') as f:
                json.dump(metadata, f, indent=2)
            
            metadata_path.chmod(0o600)
            
            logger.info(f"PDF saved for session {session_id}: {original_filename} ({len(pdf_data)} bytes)")
            return str(pdf_path)
            
        except Exception as e:
            logger.error(f"Failed to save PDF for session {session_id}: {str(e)}")
            raise
    
    
    def get_metadata(self, session_id: str) -> Optional[Dict[str, Any]]:
        try:
            metadata_path = self.get_metadata_path(session_id)
            
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
                    logger.error(f"Error processing metadata file {metadata_path.name}: {str(e)}")
                    continue
            
            # Also clean up orphaned PDF files without metadata
            pdf_files = list(self.base_dir.glob("*.pdf"))
            for pdf_path in pdf_files:
                try:
                    # Extract session ID from filename
                    if pdf_path.name.endswith('.pdf'):
                        session_id = pdf_path.name[:-4]  # Remove .pdf extension
                        metadata_path = self.get_metadata_path(session_id)
                        
                        if not metadata_path.exists():
                            # Orphaned PDF file, check its modification time
                            file_mtime = datetime.fromtimestamp(pdf_path.stat().st_mtime)
                            if file_mtime < cutoff_time:
                                pdf_path.unlink()
                                logger.info(f"Cleaned up orphaned PDF: {pdf_path.name}")
                                cleaned_count += 1
                                
                except Exception as e:
                    logger.error(f"Error cleaning up PDF file {pdf_path.name}: {str(e)}")
                    continue
            
            logger.info(f"PDF cleanup completed: removed {cleaned_count} old files")
            return cleaned_count
            
        except Exception as e:
            logger.error(f"Failed to cleanup old PDF files: {str(e)}")
            return 0
    
    def delete_session(self, session_id: str):
        try:
            pdf_path = self.base_dir / self.get_pdf_filename(session_id)
            metadata_path = self.get_metadata_path(session_id)
            
            files_deleted = 0
            if pdf_path.exists():
                pdf_path.unlink()
                files_deleted += 1
            if metadata_path.exists():
                metadata_path.unlink()
                files_deleted += 1
                
            if files_deleted > 0:
                logger.info(f"Deleted PDF session {session_id}: {files_deleted} files removed")
            
        except Exception as e:
            logger.error(f"Failed to delete PDF session {session_id}: {str(e)}")
    
    
    async def start_cleanup_task(self, cleanup_interval_hours: int = 1, max_age_hours: int = 24):
        while True:
            try:
                await asyncio.sleep(cleanup_interval_hours * 3600)
                self.cleanup_old_files(max_age_hours)
            except Exception as e:
                logger.error(f"Error in cleanup task: {str(e)}")