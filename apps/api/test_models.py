import requests
from core.config import GEMINI_API_KEY

key = GEMINI_API_KEY
res = requests.get(f'https://generativelanguage.googleapis.com/v1beta/models?key={key}')
for m in res.json().get('models', []):
    if 'flash' in m['name']:
        print(m['name'])
