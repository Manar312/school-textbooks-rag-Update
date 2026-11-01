# تشغيل المشروع على سيرفر (Ubuntu VPS)

1. **سيرفر + يوزر**: `adduser deploy && usermod -aG sudo deploy`، واستخدم SSH key فقط.
2. **Deploy key**: على السيرفر `ssh-keygen -t ed25519`، وأضف الـ public key في GitHub → Repo → Settings → Deploy keys (read-only). بعدين:
   `git clone git@github.com:Manar312/school-textbooks-rag-Update.git /srv/school-rag`.
3. **Docker**: `curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker deploy`.
4. **Firewall**: `sudo ufw default deny incoming && sudo ufw allow 22 && sudo ufw allow 80 && sudo ufw allow 443 && sudo ufw enable`.
5. **الإعدادات**: `cp .env.example .env`، واضبط كلمات المرور و`GROQ_API_KEY` و`APP_ENV=prod` و`JWT_SECRET` (`openssl rand -hex 32`) و`CORS_ORIGINS`.
6. **تشغيل**: `docker compose up -d --build`. تأكد باستخدام `docker compose ps` و`curl localhost/health/ready`.
7. **لوحات المراقبة (Grafana / Flower / Prometheus)**: من جهازك عبر SSH port forwarding:
   `ssh -L 3000:localhost:3000 -L 5555:localhost:5555 -L 9090:localhost:9090 deploy@SERVER` ثم افتح `localhost:3000` و`localhost:5555`.
8. **تشغيل تلقائي بعد restart**: انسخ `deploy/school-rag.service` إلى `/etc/systemd/system/` ثم `sudo systemctl enable --now school-rag`.
9. **GitHub Actions**: أضف Secrets باسم `SERVER_HOST`, `SERVER_USER`, `SERVER_SSH_KEY`، ومتغير `APP_DIR=/srv/school-rag`. النشر التلقائي يعمل بعد نجاح CI على `main`.
10. **Backup**: أضف cron: `0 3 * * * /srv/school-rag/deploy/backup.sh`.
11. **HTTPS**: راجع تعليمات Certbot في `deploy/nginx/nginx.conf` قبل إتاحة الخدمة على الإنترنت.

**ممنوع**: `docker compose down -v` لأنه يمسح البيانات. لا تستخدمه إلا لو كنت تقصد حذف الـ volumes والبيانات نهائيًا.
