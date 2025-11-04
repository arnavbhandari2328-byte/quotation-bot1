import os
import re
import gc
import json
import datetime
from flask import Flask, request, Response, jsonify
import requests

# --------- ENV ---------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
META_ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
META_VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN")

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")              # required on Render
RESEND_FROM    = os.environ.get("RESEND_FROM",                 # you asked to use your Gmail here
                                "Nivee Metal <arnavbhandari2328@gmail.com>")

TEMPLATE_FILE = "Template.docx"

# --------- APP ---------
app = Flask(__name__)

@app.get("/")
def root():
    return jsonify(service="quotation-bot", status="ok",
                   time=str(datetime.datetime.utcnow()) + "Z")

@app.get("/health")
def health():
    missing = [k for k, v in {
        "GEMINI_API_KEY": GEMINI_API_KEY,
        "META_ACCESS_TOKEN": META_ACCESS_TOKEN,
        "PHONE_NUMBER_ID": PHONE_NUMBER_ID,
        "META_VERIFY_TOKEN": META_VERIFY_TOKEN,
        "RESEND_API_KEY": RESEND_API_KEY
    }.items() if not v]
    return jsonify(ok=len(missing) == 0, missing=missing)

# --------- WHATSAPP SEND ---------
def send_whatsapp_reply(to_phone_number, message_text):
    if not META_ACCESS_TOKEN or not PHONE_NUMBER_ID:
        print("!!! ERROR: Meta API keys missing. Cannot send reply.")
        return
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}",
               "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to_phone_number,
               "type": "text", "text": {"body": message_text}}
    resp = None
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=20)
        resp.raise_for_status()
        print(f"✅ WhatsApp reply sent to {to_phone_number}")
    except requests.exceptions.RequestException as e:
        print(f"!!! WhatsApp send error: {e}")
        if resp is not None:
            print(f"Status {resp.status_code} Body {resp.text}")

# --------- AI PARSE (lazy Gemini) ---------
def parse_command_with_ai(command_text):
    print("Sending command to Google AI (Gemini) for parsing...")
    try:
        import google.generativeai as genai  # lazy import
        if not GEMINI_API_KEY:
            print("!!! ERROR: GEMINI_API_KEY not set.")
            return None
        genai.configure(api_key=GEMINI_API_KEY)

        model = genai.GenerativeModel('models/gemini-pro-latest')
        today = datetime.date.today().strftime('%B %d, %Y')
        system_prompt = f"""
        You are an assistant for a stainless steel trader. Extract a quotation.

        Date today: {today}

        Extract:
        - q_no
        - date (default: today's date)
        - company_name
        - customer_name
        - product
        - quantity (ONLY the number)
        - rate (number)
        - units (default "Nos")
        - hsn
        - email

        Return ONLY a single minified JSON string. No extra words or code fences.

        Example:
        User: "quote 101 for Raju at Raj pvt ltd, 500 pcs 3in pipe at 600, hsn 7304, email raju@gmail.com"
        AI: {{"q_no":"101","date":"{today}","company_name":"Raj pvt ltd","customer_name":"Raju","product":"3in pipe","quantity":"500","rate":"600","units":"Pcs","hsn":"7304","email":"raju@gmail.com"}}
        """
        response = model.generate_content(system_prompt + "\n\nUser: " + command_text)
        ai_text = (response.text or "").strip().replace("```json", "").replace("```", "").strip()
        print(f"AI response received: {ai_text}")

        context = json.loads(ai_text)

        # required fields
        for f in ['product', 'customer_name', 'email', 'rate', 'quantity']:
            if not str(context.get(f, "")).strip():
                print(f"!!! ERROR: Missing field {f}")
                return None

        # numbers
        try:
            qty = int(re.sub(r"[^\d]", "", str(context['quantity'])))
            rate = float(str(context['rate']).replace(",", "").strip())
            total = qty * rate
            context['quantity'] = str(qty)
            context['rate_formatted'] = f"₹{rate:,.2f}"
            context['total'] = f"₹{total:,.2f}"
            context['rate'] = context['rate_formatted']
        except ValueError:
            print("!!! ERROR: Invalid rate/quantity.")
            return None

        # defaults
        context.setdefault('date', today)
        context.setdefault('company_name', "")
        context.setdefault('hsn', "")
        context.setdefault('q_no', "")
        context['units'] = context.get('units') or "Nos"

        print(f"Parsed context: {context}")
        return context
    except Exception as e:
        print(f"!!! AI error: {e}")
        return None
    finally:
        gc.collect()

# --------- DOCX (lazy docxtpl) ---------
def create_quotation_from_template(context):
    doc = None
    try:
        from docxtpl import DocxTemplate  # lazy import
        script_dir = os.path.dirname(os.path.abspath(__file__))
        template_path = os.path.join(script_dir, TEMPLATE_FILE)
        doc = DocxTemplate(template_path)
        doc.render(context)

        safe_name = "".join(c for c in context['customer_name'] if c.isalnum() or c in " _-").rstrip()
        filename = f"Quotation_{safe_name}_{datetime.date.today()}.docx"
        out_path = os.path.join("/tmp", filename)  # Render-friendly tmp
        doc.save(out_path)
        print(f"✅ DOCX created: '{out_path}'")
        return out_path
    except Exception as e:
        print(f"!!! DOCX error: {e}")
        return None
    finally:
        try:
            del doc
        except:
            pass
        gc.collect()

# --------- EMAIL (Resend HTTP API) ---------
def send_email_with_attachment(recipient_email, subject, body, attachment_path):
    if not attachment_path:
        print("No attachment path; aborting email.")
        return False
    if not RESEND_API_KEY:
        print("No RESEND_API_KEY; aborting email.")
        return False

    try:
        import base64  # stdlib
        with open(attachment_path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("utf-8")

        payload = {
            "from": RESEND_FROM,                         # shows your Gmail as sender
            "reply_to": "arnavbhandari2328@gmail.com",   # replies go to your Gmail
            "to": [recipient_email],
            "subject": subject,
            "text": body,
            "attachments": [{
                "filename": os.path.basename(attachment_path),
                "content": encoded,
                "type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            }]
        }
        headers = {"Authorization": f"Bearer {RESEND_API_KEY}",
                   "Content-Type": "application/json"}

        resp = requests.post("https://api.resend.com/emails", headers=headers, json=payload, timeout=30)
        if 200 <= resp.status_code < 300:
            print(f"✅ Email sent to {recipient_email}")
            try:
                os.remove(attachment_path)
                print(f"🧹 Deleted temp file {attachment_path}")
            except Exception as e:
                print(f"Warn: could not delete temp file: {e}")
            gc.collect()
            return True
        else:
            print(f"❌ Resend error {resp.status_code}: {resp.text}")
            # Note: If you see 403 validation_error, verify a domain in Resend or
            # send only to your own mailbox while testing.
            return False
    except Exception as e:
        print(f"Email exception: {e}")
        return False

# --------- WEBHOOK ---------
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        print("Webhook GET verification...")
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        if mode == 'subscribe' and token == META_VERIFY_TOKEN:
            print("✅ Verification success")
            return Response(challenge, status=200)
        print("❌ Verification failed")
        return Response("Verification failed", status=403)

    # POST
    print("Webhook received POST (message or status)")
    customer_phone_number = None
    command_text = None

    try:
        data = request.get_json(silent=True) or {}
        if not data or 'entry' not in data or not data['entry']:
            print("No entry in webhook body; ignoring.")
            return Response(status=200)

        change = (data['entry'][0].get('changes') or [{}])[0]
        val = change.get('value', {})

        if val.get('messages'):
            msg = val['messages'][0]
            if msg.get('type') == 'text':
                customer_phone_number = msg['from']
                command_text = msg['text']['body']
            else:
                print(f"Non-text message type {msg.get('type')}; ignoring.")
                return Response(status=200)
        elif val.get('statuses'):
            st = val['statuses'][0]
            print(f"Status update: {st.get('status')} for {st.get('id')}; ignoring.")
            return Response(status=200)
        else:
            print("No messages or statuses; ignoring.")
            return Response(status=200)
    except Exception as e:
        print(f"Webhook JSON parse error: {e}")
        print(f"Raw: {request.data}")
        return Response(status=200)

    if not (customer_phone_number and command_text):
        print("No text message found; ignoring.")
        return Response(status=200)

    # --- AI parse ---
    context = parse_command_with_ai(command_text)
    if not context:
        send_whatsapp_reply(customer_phone_number,
                            "Sorry, I couldn't understand your request. Please re-check and try again.")
        return Response(status=200)

    print(f"Generating quote for {context['customer_name']} ...")
    doc_file = create_quotation_from_template(context)
    if not doc_file:
        send_whatsapp_reply(customer_phone_number,
                            "Sorry, an internal error occurred while creating your document.")
        return Response(status=200)

    subject = f"Quotation from NIVEE METAL PRODUCTS PVT LTD (Ref: {context.get('q_no', 'N/A')})"
    body = f"""
Dear {context['customer_name']},

Thank you for your enquiry.

Please find our official quotation attached.

Regards,
Harsh Bhandari
Nivee Metal Products Pvt. Ltd.
"""

    ok = send_email_with_attachment(context['email'], subject, body, doc_file)
    if ok:
        send_whatsapp_reply(customer_phone_number,
                            f"Success! Your quotation for {context['product']} has been emailed to {context['email']}.")
    else:
        send_whatsapp_reply(customer_phone_number,
                            f"Sorry, I created the quote but couldn't send the email to {context['email']}.")

    gc.collect()
    return Response(status=200)

# --------- LOCAL RUN (Render uses Gunicorn) ---------
if __name__ == "__main__":
    if not all([GEMINI_API_KEY, META_ACCESS_TOKEN, PHONE_NUMBER_ID, META_VERIFY_TOKEN, RESEND_API_KEY]):
        print("!!! WARNING: one or more env vars are missing.")
    port = int(os.environ.get("PORT", 5000))
    print(f"Starting Flask on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
