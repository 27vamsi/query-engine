# Deploying for free

The whole stack (web + Celery worker + Postgres + Redis + MinIO) fits on one
small VM with Docker Compose. You don't need real AWS. Below is the route I'd
take if I wanted it running on a public URL without paying anything, plus a
Fly.io alternative if you'd rather not babysit a box.

The genuinely-free option with enough RAM to run all five containers is **Oracle
Cloud's Always Free tier**. Their Ampere ARM instances give you up to 4 cores and
24 GB of RAM for free, indefinitely. That's overkill for this, which is nice.

---

## Option A: Oracle Cloud Always Free VM

### 1. Make the instance

- Sign up at cloud.oracle.com (a card is required for identity, Always Free
  resources aren't charged).
- Compute -> Instances -> Create Instance.
- Image: **Ubuntu 22.04**.
- Shape: **VM.Standard.A1.Flex** (Ampere/ARM). Give it 2 OCPU and 8–12 GB RAM,
  well within the free allowance.
- Add your SSH public key.
- Under networking, let it create a VCN with a public IP.

If "out of capacity" shows up for ARM (it happens), try a different availability
domain or come back later. An AMD `VM.Standard.E2.1.Micro` also works on the free
tier, it's just smaller (1 core / 1 GB), which is tight but fine for a demo with a
small dataset.

### 2. Open the ports

Two layers of firewall to deal with on Oracle.

VCN security list (in the console): add an ingress rule allowing TCP `80` and
`443` (and `8000` if you want to hit it directly before setting up TLS) from
`0.0.0.0/0`.

Then on the box itself:

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8000 -j ACCEPT
sudo netfilter-persistent save
```

Oracle's Ubuntu image ships with a restrictive iptables config, so skipping this
is the usual reason "the site won't load."

### 3. Install Docker

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
# log out and back in so the group applies
```

### 4. Get the code and set secrets

```bash
git clone <your-repo-url> query-engine
cd query-engine
cp .env.example .env
```

Edit `.env` and at minimum change these:

```
DJANGO_SECRET_KEY=<paste output of: openssl rand -hex 32>
DJANGO_DEBUG=false
DJANGO_ALLOWED_HOSTS=<your-domain-or-ip>
```

The compose file already wires Postgres, Redis, and MinIO together with their own
credentials, so you don't have to touch those unless you want to.

### 5. Bring it up

```bash
docker compose up -d --build
docker compose logs -f web        # watch it migrate and start
```

Once it's healthy, seed a dataset and register it:

```bash
docker compose exec web python scripts/seed_data.py --out ./data/trips --rows 500000 --days 7
docker compose exec web python manage.py register_dataset trips ./data/trips
docker compose exec web python manage.py createsuperuser
```

Hit `http://<public-ip>:8000`. That's the whole thing running.

### 6. A real domain + HTTPS (optional but worth it)

Point an A record at the VM's public IP. A free domain works fine
(e.g. a DuckDNS subdomain, or anything you own). Then put Caddy in front, it gets
you a Let's Encrypt cert with zero config.

Add this to `docker-compose.yml`:

```yaml
  caddy:
    image: caddy:2
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - caddy_data:/data
    depends_on:
      - web
```

Add `caddy_data:` under `volumes:`, drop the `ports:` block from the `web`
service (Caddy reaches it over the internal network), and create a `Caddyfile`:

```
your-domain.example.com {
    reverse_proxy web:8000
}
```

```bash
docker compose up -d
```

Caddy fetches and renews the certificate on its own. Now you're on
`https://your-domain.example.com`.

### Keeping costs at zero

Everything above stays inside Always Free: the A1 instance, the boot volume
(you get 200 GB of block storage free), and the bandwidth. There's no managed
database or object store in the bill because Postgres, Redis, and MinIO all run as
containers on the same VM. Just don't spin up a second paid instance and you're
fine.

---

## Option B: Fly.io

Fly is easier to operate (no VM to patch) but the moving parts cost a bit more
thought because each service is its own app/volume. Fly has a small monthly
credit that covers a tiny deployment; watch your usage if the dataset grows.

The shape that works:

- One Fly app for the Django web process (`gunicorn config.wsgi`).
- One Fly app, or a second process in the same app, for the Celery worker.
- **Postgres**: `fly postgres create` (managed).
- **Redis**: Upstash Redis via `fly redis create` (managed, has a free tier).
- **Object storage**: point the engine at Cloudflare R2 or Backblaze B2 instead
  of MinIO. Both are S3-compatible and have free tiers, and running MinIO on Fly
  means paying for a volume. Set `AWS_ENDPOINT_URL` to the R2/B2 endpoint and the
  access keys; the engine code doesn't change.

Rough steps:

```bash
fly launch --no-deploy          # generates fly.toml from the Dockerfile
fly postgres create             # note the connection info it prints
fly redis create                # Upstash
fly secrets set \
  DJANGO_SECRET_KEY=$(openssl rand -hex 32) \
  DJANGO_DEBUG=false \
  DJANGO_ALLOWED_HOSTS=your-app.fly.dev \
  POSTGRES_HOST=... POSTGRES_USER=... POSTGRES_PASSWORD=... POSTGRES_DB=... \
  REDIS_URL=redis://... \
  AWS_ENDPOINT_URL=https://<account>.r2.cloudflarestorage.com \
  AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_REGION=auto
fly deploy
fly ssh console -C "python manage.py migrate"
```

Run the worker as a second process group in `fly.toml`:

```toml
[processes]
web = "gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 2"
worker = "celery -A config worker --loglevel=info --concurrency=2"
```

Fly gives you HTTPS on `*.fly.dev` for free, so there's no Caddy step here.

---

## Option C: AWS EC2 + S3

The same Compose stack on an EC2 box, with real S3 in place of MinIO since you're
already inside AWS. A `t3.micro` is free for the first 12 months but only has 1 GB
of RAM, which is tight for five containers; `t3.small` (2 GB) is the comfortable
pick and is paid. Unlike Oracle, this is not free forever, plan for a small
monthly bill after the trial.

### 1. Launch the instance

- EC2 -> Launch Instance.
- AMI: **Ubuntu 22.04**.
- Type: **t3.small** (or `t3.micro` for the free tier with a smaller dataset).
- Create or pick a key pair for SSH.
- Allocate and associate an **Elastic IP** so the address survives reboots.

### 2. Security group

Inbound rules:

- TCP `22` from **your IP only**
- TCP `80` and `443` from `0.0.0.0/0`
- TCP `8000` from `0.0.0.0/0` (only needed before you put TLS in front)

No iptables step here, the AWS Ubuntu AMI doesn't ship the restrictive firewall
Oracle's does.

### 3. Install Docker and clone

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER          # log out and back in
git clone <your-repo-url> query-engine
cd query-engine
cp .env.example .env
```

### 4. Use S3 instead of MinIO

Create a bucket and an IAM user with access to it, then drop the `minio` service
from `docker-compose.yml` (delete the `minio:` service block, the `minio_data:`
volume, and the `minio` lines under each `depends_on`). In `.env` set:

```
DJANGO_SECRET_KEY=<openssl rand -hex 32>
DJANGO_DEBUG=false
DJANGO_ALLOWED_HOSTS=<elastic-ip-or-domain>
AWS_ACCESS_KEY_ID=<iam-key>
AWS_SECRET_ACCESS_KEY=<iam-secret>
AWS_REGION=<bucket-region>
```

Leave `AWS_ENDPOINT_URL` **unset** so PyArrow talks to AWS S3 directly instead of
MinIO. The engine code doesn't change, it's the same S3 code path.

### 5. Bring it up and seed

```bash
docker compose up -d --build
python scripts/upload_minio.py --src ./data/trips --bucket <bucket> --prefix trips
docker compose exec web python manage.py register_dataset trips s3://<bucket>/trips
docker compose exec web python manage.py createsuperuser
```

(`upload_minio.py` is just an S3 client; with `AWS_ENDPOINT_URL` unset it uploads
straight to AWS.) Hit `http://<elastic-ip>:8000`. For HTTPS, point a domain at the
Elastic IP and add the Caddy service exactly as in Option A's TLS step.

---

## Which one

If you want it genuinely free and don't mind running a VM, Oracle Cloud is the
pick, the A1 box has more headroom than this app will ever use. If you'd rather
not own a server and you're okay wiring up R2 for storage, Fly is less to babysit.
Either way the app itself is unchanged; it's all environment variables.

## A note on the dataset size

For a public demo, keep the seed small (a few hundred thousand rows). The pruning
metrics read exactly the same whether it's 300k rows or 30M, and a small dataset
keeps memory and the free-tier disk comfortable. If you want the "tens of GB"
story for an interview, generate that locally and run the benchmark on your own
machine where there's no hosting cost.
