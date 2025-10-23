#!/usr/bin/env python3

import yaml
from pathlib import Path
from shared.logger import get_logger
from ..models import PipelineConfig

logger = get_logger(__name__)

def load_config(config_path: Path) -> PipelineConfig:
    try:
        with open(config_path, 'r') as f:
            config_data = yaml.safe_load(f)

        config = PipelineConfig()

        for key, value in config_data.items():
            setattr(config, key, value)

        logger.info(f"Configuration loaded from {config_path}")
        return config

    except Exception as e:
        logger.error(f"Failed to load config from {config_path}: {e}")
        logger.info("Using default configuration")
        return PipelineConfig()
