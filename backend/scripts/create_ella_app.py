#!/usr/bin/env python3
"""Create Ella AI apps in the database."""
import sys
sys.path.insert(0, '/root/omi/backend')

from datetime import datetime, timezone
from database.apps import upsert_app_to_db

apps = [
    {
        'id': 'ella-ai-agent',
        'name': 'Ella AI',
        'author': 'Ella AI Care',
        'description': 'Your personal AI companion with memory and context awareness. Routes through n8n LLM Proxy.',
        'image': 'https://ella-ai-care.com/ella-avatar.png',
        'category': 'assistant',
        'capabilities': ['chat'],
        'chat_prompt': 'You are Ella, a helpful and empathetic AI assistant with memory capabilities.',
        'approved': True,
        'status': 'approved',
        'private': False,
        'rating_avg': 5.0,
        'rating_count': 0,
        'installs': 0,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'reviews': [],
        'connected_accounts': [],
    },
    {
        'id': 'caregiver',
        'name': 'Caregiver Assistant',
        'author': 'Ella AI Care',
        'description': 'Specialized caregiver support assistant for eldercare guidance and wellness tips.',
        'image': 'https://ella-ai-care.com/caregiver-avatar.png',
        'category': 'health',
        'capabilities': ['chat'],
        'chat_prompt': 'You are a compassionate caregiver assistant, specializing in eldercare support, wellness tips, and emotional support for caregivers.',
        'approved': True,
        'status': 'approved',
        'private': False,
        'rating_avg': 5.0,
        'rating_count': 0,
        'installs': 0,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'reviews': [],
        'connected_accounts': [],
    },
]

if __name__ == '__main__':
    for app in apps:
        print(f'Creating app: {app["name"]} (id: {app["id"]})')
        upsert_app_to_db(app)
        print(f'  Done!')
    print('\nAll apps created successfully!')
