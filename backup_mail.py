import sys, smtplib, ssl, os
from email.message import EmailMessage

path = sys.argv[1]; user = sys.argv[2]; pw = sys.argv[3]; to = sys.argv[4]
subj = sys.argv[5] if len(sys.argv) > 5 else "Backup Corexia"
m = EmailMessage()
m["From"] = user; m["To"] = to; m["Subject"] = subj
m.set_content("Backup automatico Corexia. Dados criptografados (gpg) em anexo. "
              "Para restaurar: gpg -d arquivo.gpg > dados.tar.gz ; tar xzf dados.tar.gz")
with open(path, "rb") as f:
    m.add_attachment(f.read(), maintype="application", subtype="octet-stream", filename=os.path.basename(path))
ctx = ssl.create_default_context()
with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=60) as s:
    s.login(user, pw)
    s.send_message(m)
print("email enviado para", to)
