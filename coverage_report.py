import psycopg

conn = psycopg.connect(
    "dbname=cjeu_ai user=serbansarbu host=localhost"
)

cur = conn.cursor()

print("\n=========================================")
print("CJEU COVERAGE REPORT")
print("=========================================\n")

# Decisions per year
print("Decisions per year:\n")

cur.execute("""
SELECT
    substring(celex, 2, 4) AS year,
    COUNT(DISTINCT celex) AS decisions,
    COUNT(*) AS paragraphs
FROM cjeu_paragraphs
WHERE celex ~ '^[0-9]{5}CJ[0-9]+'
GROUP BY year
ORDER BY year;
""")

rows = cur.fetchall()

total_decisions = 0
total_paragraphs = 0

for year, decisions, paragraphs in rows:
    total_decisions += decisions
    total_paragraphs += paragraphs

    print(
        f"{year}: "
        f"{decisions:4d} decisions | "
        f"{paragraphs:7d} paragraphs"
    )

print("\n-----------------------------------------")
print(f"TOTAL DECISIONS : {total_decisions}")
print(f"TOTAL PARAGRAPHS: {total_paragraphs}")
print("-----------------------------------------\n")

# Largest decisions
print("Top 10 largest decisions:\n")

cur.execute("""
SELECT
    celex,
    COUNT(*) AS paragraph_count
FROM cjeu_paragraphs
GROUP BY celex
ORDER BY paragraph_count DESC
LIMIT 10;
""")

rows = cur.fetchall()

for celex, count in rows:
    print(f"{celex}: {count} paragraphs")

print("\n=========================================\n")

cur.close()
conn.close()
