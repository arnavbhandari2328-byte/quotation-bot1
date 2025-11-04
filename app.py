import os
import re
import gc
import json
import datetime
import requests
import yagmail
from docxtpl import DocxTemplate
from flask import Flask, request, Response, jsonify
import google.generativeai as genai

# ========== ENV VARS ==========
GMAIL_USER = os.environ.get("GMAIL_USER")
GMAIL_PASS = os.environ.get("GMAIL_PASS")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
META_ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
META_VERIFY_TOKEN = os.environ.get("META_VERIFY_TOKEN")

TEMPLATE_FILE = "Template.docx"

# ========== APP ==========
app = Flask(__name__)

# Basic root + health for Railway/UptimeRobot
@app.get("/")
def root():
    return jsonify(status="ok", service="quotation-bot", time=str(datetime.datetime.utcnow()) + "Z")

@app.get("/health")
def health():
    missing = [k for k, v in {
        "GEMINI_API_KEY": GEMINI_API_KEY,
        "GMAIL_USER": GMAIL_USER,
        "GMAIL_PASS": GMAIL_PASS,
        "META_ACCESS_TOKEN": META_ACCESS_TOKEN,
        "PHONE_NUMBER_ID": PHONE_NUMBER_ID,
        "META_VERIFY_TOKEN": META_VERIFY_TOKEN
    }.items() if not v]
    return jsonify(ok=len(missing) == 0, missing=missing)

# ========== GEMINI SETUP ==========
if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"!!! CRITICAL: Could not configure Gemini API: {e}")
else:
    print("!!! CRITICAL: GEMINI_API_KEY not found in environment.")

# ========== WHATSAPP REPLY ==========
def send_whatsapp_reply(to_phone_number, message_text):
    if not META_ACCESS_TOKEN or not PHONE_NUMBER_ID:
        print("!!! ERROR: Meta API keys (TOKEN or ID) are missing. Cannot send reply.")
        return

    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_phone_number,
        "type": "text",
        "text": {"body": message_text}
    }

    response = None
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=20)
        response.raise_for_status()
        print(f"Successfully sent WhatsApp reply to {to_phone_number}")
    except requests.exceptions.RequestException as e:
        print(f"!!! ERROR sending WhatsApp reply: {e}")
        if response is not None:
            print(f"Response Status Code: {response.status_code}")
            print(f"Response Body: {response.text}")
        else:
            print("No response received from Meta API.")

# ========== AI PARSER ==========
def parse_command_with_ai(command_text):
    print("Sending command to Google AI (Gemini) for parsing...")
    try:
        model = genai.GenerativeModel('models/gemini-pro-latest')
        system_prompt = f"""
        You are an assistant for a stainless steel trader. Your job is to extract
        quotation details from a user's command.

        The current date is: {datetime.date.today().strftime('%B %d, %Y')}

        Extract the following fields:
        - q_no: The quotation number.
        - date: The date for the quote. If not mentioned, use today's date.
        - company_name: The customer's company name (e.g., "Raj Pvt Ltd").
        - customer_name: The contact person's name (e.g., "Raju").
        - product: The full product description (e.g., "3 inch SS Pipe Sch 40").
        - quantity: The numerical quantity of items (e.g., "500"). Extract only the number.
        - rate: The price per item (e.g., "600").
        - units: The unit of measurement (e.g., "Pcs", "Nos", "Kgs"). Default to "Nos" if not specified.
        - hsn: The HSN code (e.g., "7304").
        - email: The customer's email address.

        Return the result ONLY as a single, minified JSON string. Do not add any
        other text, greetings, code blocks (like ```json), or explanations.

        Example:
        User: "quote 101 for Raju at Raj pvt ltd, 500 pcs 3in pipe at 600, hsn 7304, email raju@gmail.com"
        AI: {{"q_no":"101","date":"{datetime.date.today().strftime('%B %d, %Y')}","company_name":"Raj pvt ltd","customer_name":"Raju","product":"3in pipe","quantity":"500","rate":"600","units":"Pcs","hsn":"7304","email":"raju@gmail.com"}}
        """

        full_prompt = system_prompt + "\n\nUser: " + command_text
        response = model.generate_content(full_prompt)
        ai_text = (response.text or "").strip()
        ai_text = ai_text.replace("```json", "").replace("```", "").strip()
        print(f"AI response received: {ai_text}")

        context = json.loads(ai_text)

        required = ['product', 'customer_name', 'email', 'rate', 'quantity']
        for field in required:
            if field not in context or not str(context[field]).strip():
                print(f"!!! ERROR: AI did not find a required field: '{field}' or value was empty.")
                return None

        try:
            price_num = float(str(context['rate']).replace(",", "").strip())
            qty_num = int(re.sub(r"[^\d]", "", str(context['quantity'])))
            total_num = price_num * qty_num

            context['rate_formatted'] = f"₹{price_num:,.2f}"
            context['total'] = f"₹{total_num:,.2f}"
            context['rate'] = context['rate_formatted']
            context['quantity'] = str(qty_num)
        except ValueError:
            print("!!! ERROR: AI returned 'rate' or 'quantity' as invalid numbers.")
            return None

        context.setdefault('date', datetime.date.today().strftime("%B %d, %Y"))
        context.setdefault('company_name', "")
        context.setdefault('hsn', "")
        context.setdefault('q_no', "")
        context['units'] = context.get('units') or "Nos"

        print(f"Parsed context: {context}")
        return context

    except Exception as e:
        print(f"!!! ERROR during AI processing or validation: {e}")
        return None

# ========== DOC GENERATION ==========
def create_quotation_from_template(context):
    """
    Save to /tmp to avoid filesystem quirks on hosted platforms.
    """
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        template_path = os.path.join(script_dir, TEMPLATE_FILE)
        doc = DocxTemplate(template_path)
    except Exception as e:
        print(f"!!! ERROR: Could not load template '{TEMPLATE_FILE}'. Error: {e}")
        return None

    try:
        doc.render(context)
        safe_customer_name = "".join(c for c in context['customer_name'] if c.isalnum() or c in " _-").rstrip()
        filename = f"Quotation_{safe_customer_name}_{datetime.date.today()}.docx"
        output_path = os.path.join("/tmp", filename)  # use /tmp on Railway
        doc.save(output_path)
        print(f"Successfully created '{output_path}'")
        return output_path
    except Exception as e:
        print(f"!!! ERROR rendering or saving the document: {e}")
        return None

# ========== EMAIL ==========
def send_email_with_attachment(recipient_email, subject, body, attachment_path):
    if not attachment_path:
        print("Cannot send email, no attachment was created.")
        return False

    if not GMAIL_USER or not GMAIL_PASS:
        print("!!! ERROR: GMAIL_USER or GMAIL_PASS not set. Cannot send email.")
        return False

    try:
        yag = yagmail.SMTP(GMAIL_USER, GMAIL_PASS)
        yag.send(
            to=recipient_email,
            subject=subject,
            contents=body,
            attachments=attachment_path,
        )
        print(f"Email successfully sent to {recipient_email}")
        try:
            os.remove(attachment_path)
            print(f"Cleaned up '{attachment_path}'")
        except Exception as remove_err:
            print(f"Warning: Could not remove temporary file {attachment_path}: {remove_err}")
        return True
    except Exception as e:
        print(f"Error sending email: {e}")
        return False

# ========== WEBHOOK ==========
@app.route("/webhook", methods=['GET', 'POST'])
def handle_webhook():
    # Meta verification (GET)
    if request.method == 'GET':
        print("Webhook received GET verification request...")
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        if mode == 'subscribe' and token:
            if token == META_VERIFY_TOKEN:
                print("Verification successful!")
                return Response(challenge, status=200)
            else:
                print("Verification failed: Token mismatch.")
                return Response("Verification token mismatch", status=403)
        print("Failed: Did not receive correct hub.mode or hub.verify_token")
        return Response("Failed verification", status=400)

    # Incoming messages (POST)
    print("Webhook received POST (new message or status)!")
    customer_phone_number = None
    command_text = None

    try:
        data = request.get_json(silent=True) or {}
        if not data or 'entry' not in data or not data['entry']:
            print("No entry in webhook body; ignoring.")
            return Response(status=200)

        change = (data['entry'][0].get('changes') or [{}])[0]

        if change.get('value', {}).get('messages'):
            message_data = change['value']['messages'][0]
            if message_data.get('type') == 'text':
                customer_phone_number = message_data['from']
                command_text = message_data['text']['body']
            else:
                print(f"Received non-text message type: {message_data.get('type')}. Ignoring.")
                return Response(status=200)
        elif change.get('value', {}).get('statuses'):
            status_data = change['value']['statuses'][0]
            print(f"Received status update: {status_data.get('status')} for message {status_data.get('id')}. Ignoring.")
            return Response(status=200)
        else:
            print("Received change structure without messages or statuses. Ignoring.")
            return Response(status=200)

    except Exception as e:
        print(f"Error parsing incoming JSON from Meta: {e}")
        print(f"Full raw data: {request.data}")
        return Response(status=200)

    if command_text and customer_phone_number:
        context = parse_command_with_ai(command_text)

        # Hint to GC between heavy stages
        gc.collect()

        if not context:
            print("Sorry, I couldn't understand that. (AI parsing failed)")
            send_whatsapp_reply(customer_phone_number, "Sorry, I couldn't understand your request. Please check the details and try again.")
            return Response(status=200)

        print(f"\nGenerating quote for {context['customer_name']}...")
        doc_file = create_quotation_from_template(context)
        if not doc_file:
            print("Error: Could not create the document.")
            send_whatsapp_reply(customer_phone_number, "Sorry, an internal error occurred while creating your document.")
            return Response(status=200)

        email_subject = f"Quotation from NIVEE METAL PRODUCTS PVT LTD (Ref: {context.get('q_no', 'N/A')})"
        email_body = f"""
        Dear {context['customer_name']},

        Thank you for your enquiry.

        Please find our official quotation attached...
        (Your full email text)

        Thank you,

        Harsh Bhandari
        Nivee Metal Products Pvt. Ltd.
        """

        email_sent = send_email_with_attachment(context['email'], email_subject, email_body, doc_file)

        if email_sent:
            print("Process complete!")
            reply_msg = f"Success! Your quotation for {context['product']} has been generated and sent to {context['email']}."
            send_whatsapp_reply(customer_phone_number, reply_msg)
        else:
            print("Process failed.")
            send_whatsapp_reply(customer_phone_number, f"Sorry, I created the quote but failed to send the email to {context['email']}.")

        return Response(status=200)

    print("Webhook processed but no command text found. Ignoring.")
    return Response(status=200)

# ========== LOCAL RUN ==========
if __name__ == "__main__":
    if not all([GEMINI_API_KEY, GMAIL_USER, GMAIL_PASS, META_ACCESS_TOKEN, PHONE_NUMBER_ID, META_VERIFY_TOKEN]):
        print("!!! CRITICAL: One or more required environment variables are missing.")
    else:
        print("All required API keys found in environment.")
    port = int(os.environ.get("PORT", 5000))
    print(f"Starting Flask server on host 0.0.0.0, port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
