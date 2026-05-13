# Maximum Follow-Ups Feature

## 🎯 **Problem Solved**

The AI follow-up system could send unlimited follow-up messages to unresponsive leads, potentially spamming them and damaging relationships.

## 🔧 **Solution Overview**

Implemented a **hard limit of 3 consecutive unanswered follow-ups** after which the conversation is automatically marked as "unresponsive" and closed.

## 📋 **Implementation Details**

### **1. Database Schema Changes**
- Added `unanswered_follow_up_count` field to `Deal` model
- Tracks consecutive AI follow-ups without a lead reply
- Migration: `crm.0010_add_unanswered_follow_up_count`

### **2. Counter Increment Logic**
**When AI sends a follow-up:**
```python
# In handle_follow_up()
if decision.action == "send_message":
    sent = send_raw_message(session, profile, decision.message, source="ai")
    if sent:
        deal.unanswered_follow_up_count += 1
        deal.save(update_fields=["unanswered_follow_up_count"])
```

### **3. Counter Reset Logic**
**When lead replies:**
```python
# In _update_deal_chat_summary()
def _reset_unanswered_counter_if_lead_replied(deal, new_messages):
    has_incoming = any(not msg.is_outgoing for msg in new_messages)
    if has_incoming and deal.unanswered_follow_up_count > 0:
        deal.unanswered_follow_up_count = 0
        deal.save(update_fields=["unanswered_follow_up_count"])
```

### **4. Limit Enforcement**
**Before sending follow-up:**
```python
# In handle_follow_up()
MAX_UNANSWERED_FOLLOW_UPS = 3

if deal.unanswered_follow_up_count >= MAX_UNANSWERED_FOLLOW_UPS:
    logger.info("Reached max unanswered follow-ups (%d) — marking as unresponsive", MAX_UNANSWERED_FOLLOW_UPS)
    set_profile_state(session, public_id, ProfileState.COMPLETED.value, outcome="unresponsive")
    return
```

## 🔄 **User Workflow**

### **Before (Problem)**
```
AI: "Hi John, noticed you're in SaaS..."
[No reply]
AI: "Following up - any thoughts?"
[No reply]
AI: "Checking in again..."
[No reply]
AI: "One last follow-up..."  # 😬 Could go on forever
```

### **After (Solution)**
```
AI: "Hi John, noticed you're in SaaS..."
[No reply] → count = 1
AI: "Following up - any thoughts?"
[No reply] → count = 2
AI: "Checking in again..."
[No reply] → count = 3
AI: Reached max unanswered follow-ups (3) — marking as unresponsive ✅
```

### **With Lead Reply**
```
AI: "Hi John, noticed you're in SaaS..."
[No reply] → count = 1
AI: "Following up - any thoughts?"
Lead: "Thanks, I'll review it" → count = 0 (reset)
AI: "Great, let me know if you have questions" → count = 1
```

## 🎛️ **Configuration**

### **Current Settings (Hardcoded)**
```python
MAX_UNANSWERED_FOLLOW_UPS = 3  # Maximum consecutive unanswered follow-ups
```

### **Future Enhancements**
- Configurable limit per campaign
- Different limits based on lead quality score
- Adaptive limits based on response patterns

## 📊 **Django Admin Features**

### **Deal Model Updates**
- Added `unanswered_follow_up_count` field
- Visible in Deal admin interface
- Can be manually reset if needed

## 🧪 **Testing**

### **Test Scenarios**
1. ✅ Counter increments on AI follow-up
2. ✅ Counter resets on lead reply
3. ✅ Limit enforced after 3 unanswered
4. ✅ Auto-mark as completed when limit reached
5. ✅ Counter persists across daemon restarts

### **Manual Testing**
```python
# Check counter for a deal
deal = Deal.objects.get(lead__public_identifier="john-doe", campaign_id=1)
print(f"Unanswered count: {deal.unanswered_follow_up_count}")

# Manually reset counter (if needed)
deal.unanswered_follow_up_count = 0
deal.save(update_fields=["unanswered_follow_up_count"])
```

## 🚀 **Deployment Notes**

### **Migration Required**
```bash
python manage.py migrate crm
```

### **No Breaking Changes**
- Existing deals: Counter starts at 0
- Backward compatible: Works without configuration
- Safe: Only affects new follow-up behavior

### **Performance Impact**
- Minimal: One additional DB update per follow-up
- Efficient: Counter reset only when lead replies

## 🎯 **Key Benefits**

1. **Prevents Spam**: Hard limit prevents excessive follow-ups
2. **Protects Reputation**: Avoids annoying unresponsive leads
3. **Smart Reset**: Counter resets when lead engages
4. **Automatic Cleanup**: Conversations auto-close after limit
5. **Configurable**: Easy to adjust limit if needed
6. **Transparent**: Clear logging of limit enforcement

## 📝 **Usage Examples**

### **View Counter Status**
```python
from crm.models import Deal

deal = Deal.objects.filter(
    lead__public_identifier="john-doe",
    campaign_id=1
).first()

if deal:
    print(f"Unanswered follow-ups: {deal.unanswered_follow_up_count}/3")
```

### **Manually Reset Counter**
```python
# If you want to give a lead another chance
deal.unanswered_follow_up_count = 0
deal.save(update_fields=["unanswered_follow_up_count"])
# Re-enqueue follow-up
from linkedin.tasks.scheduler import enqueue_follow_up
enqueue_follow_up(deal.campaign.pk, deal.lead.public_identifier)
```

### **Adjust Limit (Code Change)**
```python
# In linkedin/tasks/follow_up.py
MAX_UNANSWERED_FOLLOW_UPS = 5  # Change from 3 to 5
```

## 🔍 **Logging**

When limit is reached:
```
[Campaign Name] follow_up john-doe: reached max unanswered follow-ups (3) — marking as unresponsive
```

When counter resets:
```
Reset unanswered_follow_up_count for john-doe (lead replied)
```

---

**Status**: ✅ **IMPLEMENTED AND TESTED**

This feature successfully prevents excessive follow-ups while maintaining smart behavior when leads engage.
