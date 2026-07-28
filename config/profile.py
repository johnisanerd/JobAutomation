"""
Akshit Saini — Candidate Profile
Single source of truth used by all 5 agents.
"""

PROFILE = {
    "name": "Akshit Saini",
    "email": "asaini592@gmail.com",
    "phone": "+353 89 419 2749",
    "location": "Dublin, Ireland",
    "linkedin": "linkedin.com/in/akshit-saini-8b8627172",
    "github": "github.com/Akshit-Saini",
    "visa": "Stamp 1G — full-time work, no employer sponsorship required",

    "target_roles": [
        "Full-Stack Engineer", "Backend Engineer", "AI/ML Engineer",
        "Frontend Engineer", "Founding Engineer", "Software Engineer",
        "Full-Stack Developer", "ML Engineer",
    ],
    "target_locations": ["Dublin", "Ireland", "Remote", "EU Remote"],
    "salary_min_eur": 50000,
    "salary_max_eur": 80000,
    "score_threshold": 60,
    "max_roles_per_company": 3,   # apply to at most N distinct teams per company
    # Companies Agent 3 must NOT apply to anymore — already done for this profile.
    # Matched case-insensitively as a substring of the company name.
    "agent3_skip_companies": ["stripe", "anthropic", "databricks","okta","amplitude"],

    "skills": {
        "languages":  ["JavaScript", "TypeScript", "Python", "HTML", "CSS"],
        "ml_ai":      ["scikit-learn", "TensorFlow", "PyTorch", "Pandas",
                       "NumPy", "FastAPI", "Matplotlib", "Plotly"],
        "frameworks": ["React", "Next.js", "Node.js", "NestJS", "Express",
                       "Angular", "TailwindCSS", "React Native"],
        "devops":     ["Docker", "Kubernetes", "Azure", "Google Cloud Run", "CI/CD"],
        "databases":  ["PostgreSQL", "MySQL", "MongoDB", "Redis"],
        "other":      ["REST APIs", "WebSockets", "Microservices", "System Design",
                       "Machine Learning", "Data Structures & Algorithms"],
    },

    "summary": (
        "Software Developer with a Master's in Computer Science (Distinction, 1:1) "
        "from Trinity College Dublin and 2+ years building scalable, production-grade systems. "
        "Experienced in Python, JavaScript (Node.js, React), backend architecture, API design "
        "and full-stack development. Proficient in AI/ML workflows. Currently hold Stamp 1G "
        "and can work full-time in Ireland without employer sponsorship."
    ),

    "experience": [
        {
            "title":   "Founding Software Engineer",
            "company": "EEZPAYS (Stealth Fintech Startup)",
            "location":"Dublin, Ireland",
            "dates":   "Oct 2025 – Present",
            "bullets": [
                "Built a full-stack fintech platform from scratch, owning end-to-end architecture across Next.js/React frontend, Node.js/NestJS backend and PostgreSQL/Redis, delivering a stable MVP in under 5 months",
                "Designed and implemented 35+ RESTful APIs, secure authentication/session management and a dynamic wallet payment flow with SCA-ready architecture",
                "Optimised system performance to achieve sub-200ms API response times and scalable concurrent user flows through efficient caching and modular service design",
                "Led the complete product lifecycle from UX design and system architecture to deployment, building 25+ core features across payments, dashboard and integrations",
                "Engineered containerised, cloud-ready infrastructure using Docker as the sole technical owner",
            ],
        },
        {
            "title":   "Software Developer",
            "company": "Bajaj Finserv",
            "location":"Pune, India",
            "dates":   "Jun 2022 – Aug 2024",
            "bullets": [
                "Led development of 8 full-stack platforms for loan disbursement and partner onboarding using AngularJS, Node.js and MySQL, boosting application throughput by 16%",
                "Engineered secure REST APIs and containerised microservices using Docker, deployed via Azure; optimised KPI dashboards tracking <200ms response times, <0.1% error rates and 24/7 uptime",
                "Earned internal awards for leading successful deployment of loan portal within critical time constraints",
            ],
        },
        {
            "title":   "Software Developer Intern",
            "company": "Bajaj Finserv",
            "location":"Pune, India",
            "dates":   "Jan 2022 – Jun 2022",
            "bullets": [
                "Built CMS web app to automate internal processing workflows, improving system speed by 4%",
                "Automated CI/CD pipeline using Docker and Azure, reducing deployment time by 40% and minimising errors",
            ],
        },
    ],

    "education": [
        {
            "degree":  "MSc Computer Science – Intelligent Systems",
            "school":  "Trinity College Dublin",
            "dates":   "Sept 2024 – Aug 2025",
            "grade":   "Distinction (1:1)",
            "notes":   "Modules: Data Analytics, ML, AI, Adaptive Applications, Advanced Software Engineering, Knowledge Engineering, Distributed Systems. Dissertation: Timeline Visualisation System using ML and Knowledge Graphs.",
        },
        {
            "degree":  "B.Tech Computer Science",
            "school":  "SRM Institute of Science and Technology",
            "dates":   "Jul 2018 – May 2022",
            "grade":   "CGPA 9.10/10",
            "notes":   "75% merit-based scholarship throughout programme.",
        },
    ],

    "projects": [
        {
            "name": "Applied Machine Learning Projects",
            "url":  "github.com/Akshit-Saini/Applied-ML-Projects",
            "desc": "Ridge/Lasso regression, kNN classifier (96% accuracy, 0.99 AUC), GPT-based transformer, melody generation model (perplexity 3.56, outperformed Markov baseline)",
        },
        {
            "name": "NomNom Squad – Distributed Multiplayer Game",
            "url":  "github.com/NomNomSquad/nomnomgame",
            "desc": "Fault-tolerant Pac-Man-style game; Node.js microservices on Google Cloud Run; <50ms WebSocket latency via Redis + Socket.IO; PostgreSQL for persistent state",
        },
        {
            "name": "NutriFit Genie – Adaptive Nutrition & Fitness App",
            "url":  "github.com/NutriFitGenie/adaptive_application",
            "desc": "React health platform; scikit-learn personalised recommendation model; Docker + Azure deployment; MongoDB for user logs",
        },
    ],

    "certifications": [
        "SQL (IBM)", "Python for Data Science (IBM)",
        "Maths for ML (Imperial College London)", "C++", "Advanced JavaScript",
    ],

    "key_achievements": [
        "Delivered fintech MVP in under 5 months as sole technical owner",
        "35+ production APIs with sub-200ms response times",
        "16% throughput boost across 8 platforms at Bajaj Finserv",
        "40% reduction in deployment time via CI/CD automation",
        "Trinity MSc with Distinction (1:1)",
        "GPT melody model: perplexity 3.56, outperformed Markov baseline",
        "Multiplayer game with <50ms WebSocket latency on GCP",
    ],

    "extras": [
        "Represented Trinity College Dublin in Cricket (2024–Present)",
        "District-level athlete: Cricket, Football, Badminton",
        "Delegate at 23 Model United Nations conferences",
    ],
}

# ── Agent-level config ────────────────────────────────────────────────────────
GITHUB_RESUME_REPO = "Akshit-Saini/resume"   # UPDATE to your actual repo
GITHUB_RESUME_FILE = "main.tex"              # UPDATE to your .tex file path
GITHUB_BRANCH      = "main"

OUTREACH_TONE      = "concise-professional-bold"   # concise + impact numbers
COVER_LETTER_STYLE = "auto-full"                   # Claude-generated, full

SEARCH_QUERIES = [
    ("Full Stack Engineer",  "Dublin"),
    ("Backend Engineer",     "Dublin"),
    ("AI ML Engineer",       "Dublin"),
    ("Frontend Engineer",    "Dublin"),
    ("Founding Engineer",    "Dublin"),
    ("Software Engineer",    "Dublin"),
    ("Full Stack Developer", "Remote"),
    ("ML Engineer",          "Remote EU"),
]

# ── Greenhouse (verified 200 as of June 2026) ────────────────────────────────
GREENHOUSE_SLUGS = [
    # Original working
    "stripe", "intercom", "hubspot", "figma", "coinbase",
    "brex", "airbnb", "squarespace",
    # New additions (all verified)
    "databricks", "anthropic", "twilio", "datadog", "cloudflare",
    "netlify", "gitlab", "okta", "pagerduty", "mixpanel",
    "amplitude", "asana", "dropbox", "mongodb", "elastic",
]

# ── Ashby (verified 200 as of June 2026) ─────────────────────────────────────
# Many top-tier startups have migrated from Greenhouse/Lever to Ashby
ASHBY_SLUGS = [
    "linear", "notion", "supabase", "posthog", "retool",
    "dbt-labs", "hashicorp", "confluent", "grafana", "sentry",
    "loom", "coda", "vercel", "raycast", "arc",
]

# ── Lever (verified 200 as of June 2026) ─────────────────────────────────────
# Most original slugs have migrated — keeping only verified ones
LEVER_SLUGS = [
    # Add back any you find working via: curl https://api.lever.co/v0/postings/{slug}
]
