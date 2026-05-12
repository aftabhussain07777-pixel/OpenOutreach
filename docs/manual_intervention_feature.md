# Manual Intervention Detection Feature

## 🎯 **Problem Solved**

The AI follow-up system previously continued sending messages even when users manually took over conversations, leading to awkward double-messaging situations.

## 🔧 **Solution Overview**

Implemented a **conservative, per-conversation pause system** that:
- Detects when you manually message a lead
- Immediately pauses AI follow-ups for that specific conversation
- Notifies you with clear instructions to resume when needed
- Provides easy manual resume controls

## 📋 **Implementation Details**

### **1. Database Schema Changes**
- Added `source` field to `ChatMessage` model (`ai`/`manual` choices)
- Default: `manual` (conservative approach)
- Migration: `chat.0003_add_source_field`

### **2. AI Message Detection**
**AI messages are marked via two methods:**
- **Primary**: `send_raw_message()` receives `source="ai"` parameter from follow-up tasks
- **Fallback**: `sync_conversation()` correlates with `ActionLog.FOLLOW_UP` timestamps

### **3. Manual Message Detection**
**Conservative logic in `handle_follow_up()`:**
```python
def _has_manual_messages_recently(deal) -> bool:
    # Any manual outgoing message in last 24 hours = pause
    return ChatMessage.objects.filter(
        source="manual",
        is_outgoing=True,
        creation_date__gte=timezone.now() - timedelta(hours=24)
    ).exists()
```

### **4. Pause Behavior**
- **Immediate**: No re-enqueue when manual messages detected
- **Per-conversation**: Only affects the specific conversation
- **Indefinite**: Requires manual resume (no auto-resume)

### **5. Notification System**
**Logged when manual intervention detected:**
```
🤖 AI PAUSED: Manual message detected in conversation with john-doe
Message: "Thanks for the info, I'll review it"
Campaign: SaaS Analytics Outreach
To resume AI follow-ups, use Django Admin or run: python manage.py resume_follow_up 1 john-doe
```

### **6. Resume Controls**
**Two ways to resume AI follow-ups:**

#### **A) Django Admin**
- Navigate to `/admin/crm/deal/`
- Select connected conversations
- Choose "Resume AI follow-ups" action
- Bulk resume multiple conversations

#### **B) CLI Command**
```bash
python manage.py resume_follow_up <campaign_id> <public_identifier> [--delay=3600]
```

## 🔄 **User Workflow**

### **Before (Problem)**
```
AI: "Hi John, noticed you're in SaaS..."
You: "Thanks John, I'll review your proposal"
AI: "Following up - any thoughts on our solution?"  # 😬 Awkward!
```

### **After (Solution)**
```
AI: "Hi John, noticed you're in SaaS..."
You: "Thanks John, I'll review your proposal"
🤖 AI PAUSED: Manual message detected in conversation with john-doe
# No more AI messages until you resume
```

## 🎛️ **Configuration**

### **Current Settings (Hardcoded)**
- **Detection window**: 24 hours
- **Pause duration**: Indefinite (manual resume)
- **Strategy**: Conservative (any manual message triggers pause)

### **Future Enhancements**
- Configurable detection window per campaign
- Smart detection (only pause if message appears conversational)
- Auto-resume after X days of inactivity
- Email/Slack notifications

## 📊 **Django Admin Features**

### **Enhanced ChatMessage Admin**
- Added `source` and `is_outgoing` to list display
- Filter by message source (AI vs Manual)
- Clear visual distinction

### **New Deal Admin**
- Bulk "Resume AI follow-ups" action
- Filter by conversation state
- View conversation statistics

## 🧪 **Testing**

### **Test Coverage**
- ✅ Source field creation and defaults
- ✅ Manual message detection (various timeframes)
- ✅ AI message marking functionality
- ✅ Notification system
- ✅ Resume command functionality

### **Run Tests**
```bash
python manage.py test tests.test_manual_intervention
```

## 🚀 **Deployment Notes**

### **Migration Required**
```bash
python manage.py migrate chat
```

### **No Breaking Changes**
- Existing conversations: All messages default to `manual`
- AI messages: Will be marked correctly going forward
- Backward compatible: System works without configuration

### **Performance Impact**
- Minimal: One additional DB query per follow-up check
- Indexed: `creation_date` already indexed for performance

## 🎯 **Key Benefits**

1. **No Awkward Double-Messaging**: AI stops when you take over
2. **Per-Conversation Control**: Only affects conversations you touch
3. **Conservative by Default**: Safer than missing manual intervention
4. **Easy Resume**: Clear controls to hand control back to AI
5. **Full Visibility**: Notifications tell you exactly what happened
6. **Backward Compatible**: Works with existing data

## 📝 **Usage Examples**

### **Resume Single Conversation**
```bash
python manage.py resume_follow_up 1 john-doe
```

### **Resume with Custom Delay**
```bash
python manage.py resume_follow_up 1 john-doe --delay=7200  # 2 hours
```

### **Check Conversation Status**
```python
from chat.models import ChatMessage
from crm.models import Deal

deal = Deal.objects.get(lead__public_identifier="john-doe", campaign_id=1)
ct = ContentType.objects.get_for_model(deal.lead)

ai_count = ChatMessage.objects.filter(content_type=ct, object_id=deal.lead_id, source="ai").count()
manual_count = ChatMessage.objects.filter(content_type=ct, object_id=deal.lead_id, source="manual").count()

print(f"AI messages: {ai_count}, Manual messages: {manual_count}")
```

---

**Status**: ✅ **IMPLEMENTED AND TESTED**

This feature successfully solves the manual intervention problem while maintaining full control and visibility for users.
