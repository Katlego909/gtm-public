# 🤖 AI Chat Assistant - Setup & Usage Guide

## Overview
Your GTM Validator now has a fully context-aware AI chat assistant that knows EVERYTHING about each assessment session.

## ✅ What Was Built

### 1. **Core AI Engine** (`gtm/ai_chat.py`)
- **Intent Detection**: Automatically routes user queries to specialized handlers
- **Context Builder**: Extracts complete assessment data for AI consumption
- **Smart Handlers**: Pre-built responses for common queries (scores, recommendations, roadmaps)
- **General Chat**: Gemini-powered conversational AI with full context awareness

### 2. **Database Model** (`ChatMessage` in `models.py`)
- Stores conversation history
- Tracks intents for analytics
- Links messages to assessment sessions
- Enables conversation context over time

### 3. **Views & API** (`views.py`)
- `chat_view`: Renders the chat interface
- `chat_api`: JSON endpoint for message processing
- Session validation and ownership checks

### 4. **Beautiful UI** (`templates/gtm/chat.html`)
- Modern chat interface with message bubbles
- Real-time typing indicators
- Suggested prompts for quick actions
- Session context display
- Markdown rendering for AI responses
- Mobile-responsive design

### 5. **Integration Points**
- Added "🤖 Ask AI Assistant" buttons to Results and Playbook pages
- Admin interface for viewing chat history
- URL routes: `/chat/<session_id>/` and `/api/chat/<session_id>/`

---

## 🚀 Setup Instructions

### Step 1: Install Missing Dependencies
```bash
cd c:\Users\katle\Downloads\gtm_validator\gtm_validator
pip install django-browser-reload markdown
```

### Step 2: Create Database Migration
```bash
python manage.py makemigrations gtm
python manage.py migrate
```

### Step 3: Verify Gemini API Key
Ensure your `.env` file has:
```env
GEMINI_API_KEY=your_actual_api_key_here
```

### Step 4: Start the Server
```bash
python manage.py runserver
```

### Step 5: Test the Chat
1. Navigate to any completed assessment: `http://127.0.0.1:8000/results/<session_id>/`
2. Click "🤖 Ask AI Assistant"
3. Try these queries:
   - "What's the name of my company?"
   - "Show me my scores"
   - "What should I focus on?"
   - "Give me a 90-day roadmap"
   - "How do I improve conversion?"

---

## 🎯 AI Capabilities

### Built-in Intent Handlers (No AI needed - instant responses)
1. **Show Scores** - Display overall and category scores
2. **Weakest Areas** - Identify focus areas with specific questions
3. **Strongest Areas** - Highlight competitive advantages
4. **Recommendations** - Actionable advice based on gaps
5. **Roadmap** - 30-60-90 day improvement plan
6. **Export** - Links to PDF and playbook
7. **Company Info** - Assessment metadata

### Gemini-Powered General Chat
For any question not matching above intents, the AI receives:
- Complete company and industry info
- All category scores (5 levels of detail)
- Specific low-scoring questions
- Action item status
- Current maturity stage and headline
- Playbook availability

The AI is **instructed to NEVER say "I don't have access"** - it has EVERYTHING.

---

## 🔧 Troubleshooting

### Issue: "I encountered an error processing your question"
**Cause**: Gemini API issue or rate limit
**Fix**: 
1. Check your API key is valid
2. Verify internet connection
3. Check Gemini API console for quotas

### Issue: Generic/Unhelpful Responses
**Cause**: AI not using context properly
**Fix**: The system prompt has been enhanced to force context usage. If it persists:
1. Check the logs for errors
2. Verify `build_session_context()` returns full data
3. Increase the `system_prompt` specificity in `handle_general_chat()`

### Issue: Chat page not found (404)
**Cause**: URL not registered
**Fix**: Ensure `urls.py` has:
```python
path("chat/<uuid:session_id>/", views.chat_view, name="chat"),
path("api/chat/<uuid:session_id>/", views.chat_api, name="chat_api"),
```

---

## 📊 Admin Features

View all chat conversations in Django Admin:
1. Go to `/admin/gtm/chatmessage/`
2. Filter by:
   - Intent type
   - Date range
   - Session/company
3. Search messages and responses
4. Track most common queries

---

## 🎨 Customization

### Change AI Personality
Edit the system prompt in `ai_chat.py` → `handle_general_chat()`:
```python
system_prompt = f"""You are a [PERSONALITY] helping {company_name}...
```

### Add New Intent Handlers
1. Add pattern to `INTENTS` dict
2. Create `handle_your_intent()` function
3. Add to `handler_map` in `process_chat_message()`

### Modify UI
Edit `templates/gtm/chat.html`:
- Colors: Change `bg-indigo-*` classes
- Layout: Adjust flex/grid containers
- Messages: Modify `.markdown-content` styles

---

## 🚀 Next Steps - Enhanced Features

### 1. **Conversation Memory**
Enable the AI to reference previous messages:
```python
# In handle_general_chat, add:
recent_messages = ChatMessage.objects.filter(session=session).order_by('-created_at')[:5]
conversation_history = "\n".join([f"User: {m.message}\nAI: {m.response}" for m in recent_messages])
```

### 2. **Proactive Suggestions**
After each response, suggest related actions:
```python
suggestions = [
    "Want me to create action items for this?",
    "Should I draft an email to your team?",
    "Need help prioritizing these recommendations?"
]
```

### 3. **Voice Input**
Add Web Speech API to chat interface:
```javascript
const recognition = new webkitSpeechRecognition();
recognition.onresult = (event) => {
    chatInput.value = event.results[0][0].transcript;
};
```

### 4. **Export Chat History**
Add download button:
```python
def export_chat(request, session_id):
    messages = ChatMessage.objects.filter(session__uuid=session_id)
    # Generate PDF or markdown file
```

### 5. **Multi-Session Comparison**
"Compare this assessment to my previous one"
```python
previous_session = AssessmentSession.objects.filter(
    user=session.user
).exclude(uuid=session.uuid).order_by('-created_at').first()
```

---

## 📝 Example Queries Users Can Ask

### Basic Info
- "What's my company name?"
- "What industry am I in?"
- "What's my overall score?"

### Analysis
- "Why is my Demand score low?"
- "What's my biggest weakness?"
- "How do I compare to others?"
- "Which category should I fix first?"

### Action Planning
- "Create a 30-day plan"
- "What tools should I use?"
- "How do I improve conversion?"
- "Help me prioritize my action items"

### Strategic
- "What's my ROI if I improve X?"
- "How long to reach 'Best-in-Class'?"
- "What should I tell my board?"
- "Draft a budget justification"

---

## 🎉 Success!

You now have an **app-wide AI agent** that:
✅ Knows everything about each assessment
✅ Never gives unhelpful "I don't know" responses
✅ Provides instant answers for common queries
✅ Uses Gemini for complex reasoning
✅ Maintains conversation history
✅ Integrates seamlessly with your existing UI

**Test it now by asking: "What's the name of my company?"** 🚀
