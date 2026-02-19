import re

def normalize_skill(skill_name: str) -> str:
    """
    Converts a skill name to a canonical slug.
    Rule: Lowercase, strip whitespace, replace non-alphanumeric (except + and #) with underscores.
    Preserves: 'c++', 'c#', 'node.js' -> 'node_js' (dots usually handled by regex depending on pref)
    
    Example: 
    "Apache Spark " -> "apache_spark"
    "C++" -> "c++"
    "Node.js" -> "node_js"
    """
    if not skill_name:
        return ""
    
    # Lowercase and strip
    s = skill_name.lower().strip()
    
    # Replace dots and spaces with underscores
    s = re.sub(r'[\s\.]+', '_', s)
    
    # Remove everything else that isnt alphanumeric, underscore, +, or #
    s = re.sub(r'[^a-z0-9_+#]', '', s)
    
    return s.strip('_')
