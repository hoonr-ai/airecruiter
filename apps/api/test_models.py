import os
import requests
from dotenv import load_dotenv
load_dotenv()
key = os.getenv('GEMINI_API_KEY')
res = requests.get(f'https://generativelanguage.googleapis.com/v1beta/models?key={key}')
for m in res.json().get('models', []):
    if 'flash' in m['name']:
        print(m['name'])
