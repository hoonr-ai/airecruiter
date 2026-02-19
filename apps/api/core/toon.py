from typing import Any, Dict, List, Union

def encode(data: Any, indent_level: int = 0) -> str:
    """
    Encodes a Python object (Dict, List, Pydantic Model) into TOON format.
    TOON = Token-Oriented Object Notation.
    - Minimal punctuation (no braces/commas).
    - Indentation denotes nesting.
    - Header rows for lists of objects.
    """
    indent = "  " * indent_level
    
    if isinstance(data, dict):
        lines = []
        for k, v in data.items():
            if v is None: continue
            if isinstance(v, (dict, list)) and not v: continue # Skip empty containers
            
            # Recurse
            encoded_v = encode(v, indent_level + 1)
            
            # If value is simple/single-line, keep on same line
            if "\n" not in encoded_v and len(encoded_v) < 80:
                lines.append(f"{indent}{k}: {encoded_v.strip()}")
            else:
                lines.append(f"{indent}{k}:")
                lines.append(encoded_v)
        return "\n".join(lines)
        
    elif isinstance(data, list):
        if not data: return ""
        
        # Heuristic: If list of primitive strings, comma separate
        if isinstance(data[0], str) and len(str(data[0])) < 50:
             return f"{indent}" + ", ".join(data)
             
        # If list of complex objects, use array notation
        lines = []
        for item in data:
            lines.append(f"{indent}-")
            lines.append(encode(item, indent_level + 1))
        return "\n".join(lines)
        
    elif hasattr(data, "model_dump"): # Pydantic v2
        return encode(data.model_dump(exclude_none=True), indent_level)
    elif hasattr(data, "dict"): # Pydantic v1
        return encode(data.dict(exclude_none=True), indent_level)
        
    else:
        # Primitive
        return f"{indent}{str(data)}"
