#!/usr/bin/env python3

from pathlib import Path
from typing import Dict
from shared.logger import get_logger

logger = get_logger(__name__)

def setup_output_directories(config, discovery_dir: Path) -> Dict[str, Path]:
    base_dir = discovery_dir / config.output.get('base_dir', 'output')
    subdirs = config.output.get('subdirs', {})

    output_paths = {
        'base': base_dir,
        'reports': base_dir / subdirs.get('reports', 'reports'),
        'visualizations': base_dir / subdirs.get('visualizations', 'visualizations'),
        'dendrograms': base_dir / subdirs.get('dendrograms', 'dendrograms'),
        'checkpoints': base_dir / subdirs.get('checkpoints', 'checkpoints'),
        'exports': base_dir / subdirs.get('exports', 'exports')
    }

    for path in output_paths.values():
        path.mkdir(parents=True, exist_ok=True)

    logger.info(f"Output directories created under: {output_paths['base']}")

    return output_paths
