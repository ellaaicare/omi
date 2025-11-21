# Upstream Merge Strategy - Modular n8n Integration

**Date**: November 21, 2025
**Issue**: We need to regularly pull from upstream OMI, but we've added n8n integration
**Goal**: Keep our code modular so upstream merges don't require major re-integration

---

## 🎯 **The Challenge**

**Upstream OMI Repo**: https://github.com/BasedHardware/omi
- Active development (multiple commits per day)
- We need their bug fixes and new features
- We've modified their code to call n8n instead of local LLM

**Our Modifications**:
1. `utils/llm/memories.py` - Lines 61-105: Call n8n memory agent
2. `utils/llm/conversation_processing.py` - Lines 376-398: Call n8n summary agent
3. `routers/transcribe.py` - Lines 920-950: Send to n8n scanner
4. `routers/ella.py` - NEW FILE: Callback endpoints

**Problem**: Upstream changes to these files require manual merge conflict resolution

---

## ✅ **Solution: Plugin Architecture**

Create a modular plugin system that keeps our n8n code separate from upstream code.

### **Strategy 1: Configuration-Based LLM Router** (RECOMMENDED)

**File Structure**:
```
backend/
├── utils/
│   ├── llm/
│   │   ├── memories.py          # ← Upstream file (minimal changes)
│   │   ├── conversation_processing.py  # ← Upstream file (minimal changes)
│   │   └── providers/           # ← NEW: Our plugin directory
│   │       ├── __init__.py
│   │       ├── base.py          # Abstract base class
│   │       ├── local_llm.py     # Original OMI LLM (upstream)
│   │       └── n8n_provider.py  # Our n8n integration ← OUR CODE HERE
│   └── config.py                # LLM provider configuration
```

**Benefits**:
- ✅ Upstream changes to `memories.py` won't affect our n8n code
- ✅ We can switch providers via environment variable
- ✅ Easy to add more providers later (OpenAI direct, Anthropic, etc.)
- ✅ All our n8n logic in ONE file (`n8n_provider.py`)

---

### **Implementation**

#### **Step 1: Create Base Provider Interface**

**File**: `utils/llm/providers/base.py`

```python
from abc import ABC, abstractmethod
from typing import List, Optional
from models.memories import Memory
from models.conversation import Structured
from models.transcript_segment import TranscriptSegment

class LLMProvider(ABC):
    """Base class for all LLM providers (local, n8n, OpenAI direct, etc.)"""

    @abstractmethod
    def extract_memories(
        self,
        uid: str,
        segments: List[TranscriptSegment],
        user_name: Optional[str] = None,
        memories_str: Optional[str] = None
    ) -> List[Memory]:
        """Extract memories from conversation segments"""
        pass

    @abstractmethod
    def generate_summary(
        self,
        transcript: str,
        started_at,
        language_code: str,
        tz: str,
        uid: str,
        conversation_id: str,
        **kwargs
    ) -> Optional[Structured]:
        """Generate conversation summary"""
        pass
```

---

#### **Step 2: Implement Local LLM Provider (Upstream)**

**File**: `utils/llm/providers/local_llm.py`

```python
from .base import LLMProvider
from typing import List, Optional
from langchain_core.output_parsers import PydanticOutputParser
# ... other imports

class LocalLLMProvider(LLMProvider):
    """Original OMI local LLM implementation"""

    def extract_memories(self, uid, segments, user_name=None, memories_str=None):
        """Original local LLM memory extraction (from upstream)"""
        # Copy entire original implementation from utils/llm/memories.py
        # Lines 109-130 (the local LLM fallback code)
        try:
            parser = PydanticOutputParser(pydantic_object=Memories)
            chain = extract_memories_prompt | llm_mini | parser
            response: Memories = chain.invoke({...})
            return response.facts
        except Exception as e:
            print(f'Error extracting facts: {e}')
            return []

    def generate_summary(self, transcript, started_at, language_code, tz, uid, conversation_id, **kwargs):
        """Original local LLM summary generation (from upstream)"""
        # Copy entire original implementation
        # This becomes the fallback when n8n fails
        return original_llm_summary(...)
```

---

#### **Step 3: Implement n8n Provider (OUR CODE)**

**File**: `utils/llm/providers/n8n_provider.py`

```python
from .base import LLMProvider
from .local_llm import LocalLLMProvider  # Fallback
from typing import List, Optional
import requests

class N8NProvider(LLMProvider):
    """n8n/Letta integration provider - OUR CUSTOM CODE"""

    def __init__(self, fallback_to_local: bool = True):
        self.fallback_provider = LocalLLMProvider() if fallback_to_local else None
        self.n8n_base = "https://n8n.ella-ai-care.com/webhook"

    def extract_memories(self, uid, segments, user_name=None, memories_str=None):
        """Call n8n memory agent, fallback to local LLM if fails"""
        try:
            print(f"📤 Calling n8n memory agent for uid={uid}")

            segments_data = [
                {"text": s.text, "speaker": s.speaker or f"SPEAKER_{s.speaker_id}"}
                for s in segments
            ]

            response = requests.post(
                f"{self.n8n_base}/memory-agent",
                json={"uid": uid, "segments": segments_data},
                timeout=120
            )

            if response.status_code == 200:
                result = response.json()

                # Check if async mode
                if not result or result.get('status') == 'processing':
                    print(f"⏳ n8n memory agent processing asynchronously")
                    return []  # Callback will create memories later

                # Sync mode - immediate result
                memories_list = result.get('memories', [])
                print(f"✅ n8n returned {len(memories_list)} memories")

                memories = []
                for mem in memories_list:
                    memory = Memory(
                        content=mem['content'],
                        category=MemoryCategory(mem.get('category', 'interesting')),
                        visibility=mem.get('visibility', 'private'),
                        tags=mem.get('tags', [])
                    )
                    memories.append(memory)

                return memories
            else:
                print(f"⚠️  n8n returned {response.status_code}, falling back")
                raise Exception(f"n8n error: {response.status_code}")

        except Exception as e:
            print(f"⚠️  n8n memory agent failed: {e}")

            # Fallback to local LLM
            if self.fallback_provider:
                print(f"🔄 Using local LLM fallback")
                return self.fallback_provider.extract_memories(
                    uid, segments, user_name, memories_str
                )
            return []

    def generate_summary(self, transcript, started_at, language_code, tz, uid, conversation_id, **kwargs):
        """Call n8n summary agent, fallback to local LLM if fails"""
        try:
            print(f"📤 Calling n8n summary agent for uid={uid}")

            response = requests.post(
                f"{self.n8n_base}/summary-agent",
                json={
                    "uid": uid,
                    "conversation_id": conversation_id,
                    "transcript": transcript,
                    "started_at": started_at.isoformat(),
                    "language_code": language_code,
                },
                timeout=120
            )

            if response.status_code == 200:
                result = response.json()

                # Check if async mode
                if not result or result.get('status') == 'processing':
                    print(f"⏳ n8n summary agent processing asynchronously")
                    return None  # Callback will update later

                # Sync mode - immediate result
                print(f"✅ n8n returned summary: {result.get('title', 'N/A')}")
                return self._parse_summary(result)
            else:
                raise Exception(f"n8n error: {response.status_code}")

        except Exception as e:
            print(f"⚠️  n8n summary agent failed: {e}")

            # Fallback to local LLM
            if self.fallback_provider:
                print(f"🔄 Using local LLM fallback")
                return self.fallback_provider.generate_summary(
                    transcript, started_at, language_code, tz, uid, conversation_id, **kwargs
                )
            return None

    def _parse_summary(self, data):
        """Parse n8n summary response to Structured object"""
        # ... conversion logic ...
        return Structured(...)
```

---

#### **Step 4: Configuration**

**File**: `utils/config.py`

```python
import os
from .llm.providers.base import LLMProvider
from .llm.providers.local_llm import LocalLLMProvider
from .llm.providers.n8n_provider import N8NProvider

def get_llm_provider() -> LLMProvider:
    """Get configured LLM provider based on environment"""
    provider_name = os.getenv("LLM_PROVIDER", "n8n").lower()

    if provider_name == "n8n":
        return N8NProvider(fallback_to_local=True)
    elif provider_name == "local":
        return LocalLLMProvider()
    else:
        raise ValueError(f"Unknown LLM provider: {provider_name}")

# Singleton instance
llm_provider = get_llm_provider()
```

**Environment Variable** (`.env`):
```bash
# Choose LLM provider: "n8n", "local", or "openai_direct"
LLM_PROVIDER=n8n
```

---

#### **Step 5: Modify Upstream Files (MINIMAL CHANGES)**

**File**: `utils/llm/memories.py`

```python
# At top of file
from utils.config import llm_provider  # NEW: Get configured provider

def new_memories_extractor(uid, segments, user_name=None, memories_str=None):
    # ... existing validation ...

    # REPLACE entire n8n integration block (lines 61-130) with:
    return llm_provider.extract_memories(uid, segments, user_name, memories_str)
```

**That's it!** Just 1 line changed in upstream file.

---

**File**: `utils/llm/conversation_processing.py`

```python
# At top of file
from utils.config import llm_provider  # NEW

def get_transcript_structure(transcript, started_at, language_code, tz, uid, conversation_id, **kwargs):
    # ... existing code ...

    # REPLACE entire n8n block (lines 376-400) with:
    return llm_provider.generate_summary(
        transcript, started_at, language_code, tz, uid, conversation_id, **kwargs
    )
```

**That's it!** Just 1 line changed in upstream file.

---

### **What This Achieves**

#### **Before** (Current - BAD for merges):
- ✅ n8n code embedded in upstream files
- ❌ Upstream changes = merge conflicts
- ❌ Hard to switch providers
- ❌ Can't easily add new providers

#### **After** (Modular - GOOD for merges):
- ✅ n8n code in separate plugin file
- ✅ Upstream changes = minimal conflicts (1 line per file)
- ✅ Switch providers via env var
- ✅ Easy to add providers (just create new plugin)

---

### **Upstream Merge Workflow**

```bash
# 1. Pull from upstream
git remote add upstream https://github.com/BasedHardware/omi
git fetch upstream
git merge upstream/main

# 2. Resolve conflicts (only in 2 files now!)
# utils/llm/memories.py - Line where we call llm_provider
# utils/llm/conversation_processing.py - Line where we call llm_provider

# 3. Test with our n8n provider
LLM_PROVIDER=n8n python scripts/test_n8n_e2e_full.py

# 4. Test with local LLM (ensure we didn't break upstream)
LLM_PROVIDER=local python scripts/test_omi_device_simulation.py
```

---

## 📋 **Migration Checklist**

- [ ] Create `utils/llm/providers/` directory
- [ ] Create `base.py` with LLMProvider interface
- [ ] Create `local_llm.py` with original OMI code
- [ ] Create `n8n_provider.py` with our n8n integration
- [ ] Create `utils/config.py` with provider factory
- [ ] Update `utils/llm/memories.py` (1 line change)
- [ ] Update `utils/llm/conversation_processing.py` (1 line change)
- [ ] Add `LLM_PROVIDER=n8n` to `.env`
- [ ] Test with n8n provider
- [ ] Test with local provider
- [ ] Update documentation

---

## 🔄 **Alternative Strategy 2: Git Patches**

If plugin architecture is too much refactoring:

**Create patches for our changes**:
```bash
# Save our changes as patches
git diff upstream/main utils/llm/memories.py > patches/n8n_memories.patch
git diff upstream/main utils/llm/conversation_processing.py > patches/n8n_summary.patch

# After upstream merge:
git merge upstream/main
git apply patches/n8n_memories.patch
git apply patches/n8n_summary.patch

# Resolve conflicts if any, then commit
```

**Pros**:
- ✅ No refactoring needed
- ✅ Can automate with script

**Cons**:
- ❌ Still requires conflict resolution
- ❌ Patches may break if upstream changes significantly
- ❌ Less modular

---

## 🎯 **Recommendation**

**Use Plugin Architecture (Strategy 1)**:
- Initial effort: 2-3 hours
- Long-term benefit: Minimal merge conflicts
- Bonus: Can switch providers easily
- Bonus: Clean separation of upstream vs our code

**Implementation Timeline**:
- Hour 1: Create base classes and local LLM provider
- Hour 2: Create n8n provider (move existing code)
- Hour 3: Update upstream files, test both providers

---

**Next**: Create similar modular approach for iOS debug flag integration.
