# Dockerfile

# --- Stage 1: Build Environment ---
# This stage installs build-time dependencies and Python packages.
FROM python:3.9-slim-buster as builder

# Set the working directory for the build stage
WORKDIR /app

# Install system dependencies required for building some Python packages (e.g., beautifulsoup4's lxml parser)
# and for fetching external resources (curl, git for potential future needs).
# We chain commands and clean up apt cache in a single RUN instruction to reduce image layers.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    git \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Copy only the requirements file first to leverage Docker layer caching.
# If requirements.txt doesn't change, this step and pip install won't rerun.
COPY requirements.txt .

# Install Python dependencies.
# --no-cache-dir prevents pip from storing cache in the image, saving space.
RUN pip install --no-cache-dir -r requirements.txt

# --- Stage 2: Runtime Environment ---
# This stage creates the final, lean production image.
# It only copies the necessary application code and installed Python dependencies.
FROM python:3.9-slim-buster

# Set the working directory for the runtime stage
WORKDIR /app

# Copy the installed Python packages from the builder stage
# This is crucial for a thin image as it avoids copying build tools.
COPY --from=builder /usr/local/lib/python3.9/site-packages /usr/local/lib/python3.9/site-packages
# Copy executables from site-packages (if any)
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy the application code into the final image
COPY . .

# Expose the port the Flask app runs on
EXPOSE 8000

# Command to run the application when the container starts
CMD ["python", "app.py"]
