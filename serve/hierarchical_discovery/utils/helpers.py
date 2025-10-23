#!/usr/bin/env python3

from typing import List, Dict

def serialize_list_for_csv(items: List[str], delimiter: str = ' | ') -> str:
    if not items:
        return ''
    cleaned_items = []
    for item in items:
        item_str = str(item).strip()
        if item_str.startswith('[') and item_str.endswith(']'):
            item_str = item_str[1:-1]
        cleaned_items.append(item_str.strip())
    return delimiter.join(cleaned_items)

def extract_coordinates(msg) -> Dict[str, float]:
    if hasattr(msg, 'embeddings') and hasattr(msg.embeddings, 'embedding_3d') and msg.embeddings.embedding_3d is not None and len(msg.embeddings.embedding_3d) == 3:
        coords = msg.embeddings.embedding_3d
        return {'x': float(coords[0]), 'y': float(coords[1]), 'z': float(coords[2])}

    if hasattr(msg, 'embeddings') and hasattr(msg.embeddings, 'embedding_3d') and msg.embeddings.embedding_3d is not None and len(msg.embeddings.embedding_3d) >= 3:
        coords = msg.embeddings.embedding_3d
        return {'x': float(coords[0]), 'y': float(coords[1]), 'z': float(coords[2])}

    if hasattr(msg, 'embeddings') and hasattr(msg.embeddings, 'embedding_300d') and msg.embeddings.embedding_300d is not None and len(msg.embeddings.embedding_300d) >= 3:
        coords = msg.embeddings.embedding_300d
        return {'x': float(coords[0]), 'y': float(coords[1]), 'z': float(coords[2])}

    if hasattr(msg, 'embeddings') and hasattr(msg.embeddings, 'embedding_3072d') and msg.embeddings.embedding_3072d is not None and len(msg.embeddings.embedding_3072d) >= 3:
        coords = msg.embeddings.embedding_3072d
        return {'x': float(coords[0]), 'y': float(coords[1]), 'z': float(coords[2])}

    return {'x': 0.0, 'y': 0.0, 'z': 0.0}
