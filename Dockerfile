FROM python:3.11-slim

WORKDIR /app

# The always-on image gets the optional local AI too (it has a writable disk and
# no function-size ceiling). Vercel installs the lean requirements.txt instead.
COPY requirements.txt requirements-local.txt ./
RUN pip install --no-cache-dir -r requirements-local.txt

COPY . .

EXPOSE 8000

CMD ["python", "main.py"]
