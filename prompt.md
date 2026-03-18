NovaSeat
Industry: B2B SaaS — Corporate Event & Conference Management Software
What they sell: A cloud platform that helps mid-to-large companies plan, manage, and analyze internal and external corporate events — town halls, sales kickoffs, client summits, product launches. Think "Salesforce for event operations."

Pricing model: Annual subscription, tiered by number of events/month and number of seats. Plans range from €8,000/year (Starter) to €120,000/year (Enterprise).
Customer profile: HR teams, Marketing ops, Executive Assistants, Event Managers at companies with 200–5,000 employees. Mostly in Europe and North America.

Key metrics NovaSeat tracks:
- Events created per month
- Attendee engagement scores
- Integrations active (Mail, Zoom, Catering vendors)
- Support tickets opened
- Last login date per user
- NPS score (collected quarterly)
- Feature adoption rate (e.g. "AI agenda builder", "live polling", "post-event analytics")

Churn context: NovaSeat loses ~18% of its SMB customers annually and ~7% of Enterprise. Main churn reasons historically: low platform adoption after onboarding, budget cuts, and switching to cheaper point solutions.

# The Product:
Create an agent that automatically finds the clients that wants to leave the platform and then finds a strategy to improove the engagement of the clinet with the platform.

# The Dataset
The best real-world dataset to base this on is the IBM Telco Customer Churn Dataset — one of the most widely used churn datasets in ML, with a structure that maps very naturally onto a B2B SaaS context like NovaSeat.
Where to find it:

# Google Colab — The Prediction Engine
The colab code is already ready in the colab folder.

# n8n — The Nervous System
n8n runs 4 distinct workflows in this stack:

## Workflow 1 — Nightly Model Trigger

Runs at 02:00 AM via Cron node
Calls Google Colab API to execute the churn scoring notebook
Waits for completion, validates output
Confirms scores have been written to the database
Sends a mail notification to the data team if the run fails


## Workflow 2 — Churn Alert Monitor

Polls from the database every once a day in the morning or when triggered, for accounts where risk_tier changed to "High" or "Critical"
Filters out accounts already in an active intervention (to avoid duplicate outreach)
For each newly flagged account, pulls the full account profile + churn drivers
Generate a personalized outreach message (using the churn drivers as context)
Create a follow-up Task assigned to the assigned sales manager
Update the Account record with intervention status
Trigger a "Success Call Offer" flow — books a slot directly in the CSM's calendar
Escalate to human by creating a high-priority Case if the account ARR is above €50K

Decision logic (simplified):
IF risk_tier = "Critical" AND ARR > €50K
  → Immediate CSM escalation + parallel Mail outreach

IF risk_tier = "Critical" AND ARR ≤ €50K
  → Autonomous Mail outreach + offer success call

IF risk_tier = "High"
  → Personalized mail message with specific product tip
    based on the top churn driver

IF risk_tier = "Medium"
  → Add to nurture sequence, no immediate action

Format the encriched data to a predetermined html mail template 
Sends the email to the customer

## Workflow 3 — Escalation & Follow-up

Monitors the mail to see if it is a respone for conversation outcomes
If a customer hasn't responded to outreach in 48 hours → triggers a second-touch via a different channel (email fallback)
If the conversation ended without resolution → creates a high-priority task in the database for the CSM
If the customer responded positively → updates the account's risk tier and logs the win, update also the CSM


## Workflow 4 — Weekly Reporting

Every Monday morning or when triggered, aggregates intervention outcomes from the past week
Calculates: accounts intercepted, conversations started, churn prevented (estimated), CSM escalations
Formats and sends a digest report to the Head of Customer Success via Mail message


## informations on how to write a message
Opening message — personalized, references the specific issue (e.g. "Hi Sarah, we noticed NovaSeat hasn't been used for your last 3 scheduled events — we'd love to help you get back on track")
Triage branch — detects if the customer is frustrated, confused, budget-constrained, or just busy
Resolution paths:

"Show me how" → triggers an interactive product walkthrough
"I have a problem" → opens a ticket in the application automatically
"We're reconsidering our budget" → escalates immediately to CSM with full context
"Book a call" → integrates with CSM calendar, confirms slot in the Mail conversation


Sentiment analysis — flags negative sentiment in real time and alerts the CSM via Mail message
Handoff to human — seamless transfer to a live CSM with full conversation transcript pre-loaded in the mail


