# Databricks notebook source
# MAGIC %md 
# MAGIC You may find this series of notebooks at https://github.com/databricks-industry-solutions/dns-analytics. For more information about this solution accelerator, visit https://www.databricks.com/solutions/accelerators/threat-detection.

# COMMAND ----------

# MAGIC %scala
# MAGIC displayHTML("""<iframe src="https://drive.google.com/file/d/1ZMu8nFMuCzPZonOJmib8TpFR9JNypS0L/preview" frameborder="0" height="480" width="640"></iframe>
# MAGIC """)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Data layout
# MAGIC 
# MAGIC In this workshop we're using prepared data. We use multiple tables to stage, schematize and store analytics results. Here is the TLDR on table naming:
# MAGIC - **Bronze**: Raw data
# MAGIC - **Silver**: Schematized and enriched data
# MAGIC - **Gold**:  Detections and alerts
# MAGIC 
# MAGIC Why do this? Short version: so you can always go back to the source, refine your analytics over time, and never lose any data. <a href="https://databricks.com/blog/2019/08/14/productionizing-machine-learning-with-delta-lake.html"> And the long version.</a>

# COMMAND ----------

# MAGIC %run ./00_Shared_Include

# COMMAND ----------

# MAGIC %md
# MAGIC 
# MAGIC ## Fetching the data & initial model for workshop

# COMMAND ----------

# MAGIC %md 
# MAGIC In this segment, we'll download all of the datasets we need in order to be able to run our notebook.
# MAGIC These datasets include:
# MAGIC * anonymized DNS data
# MAGIC * a GeoIP lookup database
# MAGIC * a threat feed
# MAGIC * domains generated by `dnstwist` for our enrichment pipeline
# MAGIC 
# MAGIC We also include:
# MAGIC * the top 100k domains on alexa
# MAGIC * a list of dictionary words
# MAGIC * a list of dga domains to train a DGA model

# COMMAND ----------

# MAGIC %sh 
# MAGIC if [ -d /tmp/dns-notebook-datasets ]; then
# MAGIC   cd /tmp/dns-notebook-datasets
# MAGIC   git pull
# MAGIC else
# MAGIC   cd /tmp
# MAGIC   git clone --depth 1 https://github.com/zaferbil/dns-notebook-datasets.git
# MAGIC fi

# COMMAND ----------

# Copy the downloaded data into the FileStore for this workspace
print(f'Copying datasets and model to the DBFS: {get_default_path()}')
dbutils.fs.cp("file:///tmp/dns-notebook-datasets/data", f"dbfs:{get_default_path()}/datasets/",True)
dbutils.fs.cp("file:///tmp/dns-notebook-datasets/model", f"dbfs:{get_default_path()}/model/",True)

# COMMAND ----------

# MAGIC %md 
# MAGIC 
# MAGIC ## Loading pDNS data

# COMMAND ----------

# Defining the schema for pDNS.
# You can use either the python style syntax or the SQL DDL syntax to define your schema.

# from pyspark.sql.types import StructType, StructField, StringType, LongType, StringType, ArrayType
# pdns_schema = (StructType()
#     .add("rrname", StringType(), True)
#     .add("rrtype", StringType(), True)
#     .add("time_first", LongType(), True)
#     .add("time_last", LongType(), True)
#     .add("count", LongType(), True)
#     .add("bailiwick", StringType(), True)
#     .add("rdata", ArrayType(StringType(), True), True)
# )

pdns_schema = """
  rrname     string,
  rrtype     string,
  time_first long,
  time_last  long,
  count      long,
  bailiwick  string,
  rdata      array<string>
"""

# COMMAND ----------

# In this segment, we are specifying where the data is and what type of data it is.
# You can see the json format, the path and the AWS region
df = spark.read.format("json").schema(pdns_schema).load(f"{get_default_path()}/datasets/dns_events.json")

# COMMAND ----------

# The rdata field has an array element. This isn't very useful if you want to parse it, or search in it.
# So we create a new field called rdatastr. You can see the difference in the two fields in the sample output below.
from pyspark.sql.functions import col, concat_ws
df_enhanced = df.withColumn("rdatastr", concat_ws(",", col("rdata")))
display(df_enhanced)

# COMMAND ----------

# Here we specify the format of the data to be written, and the destination path
# This is still just setup - Data has not been posted to the Bronze table yet. 
df_enhanced.write.format("delta").mode("overwrite").option("mergeSchema", "true").saveAsTable("bronze_dns")

# COMMAND ----------

# MAGIC %md
# MAGIC ## URLHaus threat feed setup
# MAGIC We will be using URLHaus threat feeds with our pDNS data. This section shows you how to ingest the URLHaus feed.
# MAGIC 
# MAGIC For this setup, we need to do two things:
# MAGIC - Define functions for field extractions so we can extract the `registered_domain_extract`, `domain_extract` and `suffix_extract` fields from the URLHaus feeds. This is done via [user defined functions (UDF)](https://docs.databricks.com/spark/latest/spark-sql/udf-python.html) that are declared in the `./Shared_Include` notebook.
# MAGIC - Create an enriched schema and save it to a silver table.

# COMMAND ----------

# We specify the source location of the URLHaus feed, the csv format, and declare that the csv has field labels in a header
threat_feeds_location = f"{get_default_path()}/datasets/ThreatDataFeed.txt"
threat_feeds_raw = spark.read.csv(threat_feeds_location, header=True)
# Display a sample so we can check to see it makes sense
display(threat_feeds_raw)

# COMMAND ----------

# We create a new enrched view by extracting the domain name from the URL using the domain_extractor user defined function from the previous section.
threat_feeds_raw.createOrReplaceTempView("threat_feeds_raw")
threat_feeds_enriched_df = spark.sql("""
  select *, domain_extract(url) as domain
  from threat_feeds_raw
  """).filter("char_length(domain) >= 2")
# The sample display shows the new field "domain"
display(threat_feeds_enriched_df)

# COMMAND ----------

# We save our new, enriched schema 
(threat_feeds_enriched_df.write
  .format("delta")
  .mode('overwrite')
  .option("mergeSchema", True)
  .saveAsTable("silver_threat_feeds")
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## DNS Twist Setup for detecting lookalike domains
# MAGIC We will use <a href="https://github.com/elceef/dnstwist">dnstwist</a> to monitor lookalike domains that adversaries can use to attack you. 
# MAGIC Using <a href="https://github.com/elceef/dnstwist">dnstwist</a> you can detect <a href="https://capec.mitre.org/data/definitions/630.html">typosquatters</a>, phishing attacks, fraud, and brand impersonation. Before using the remainder of section 1.b of this notebook, you will have to use <a href="https://github.com/elceef/dnstwist">dnstwist instructions</a> (outside of this notebook) to create a `domains_dnstwists.csv`. In our example (below) we generated variations for `google.com` using `dnstwist`. You can automate this for your own organization or for any organization of interest. 
# MAGIC 
# MAGIC After installing `dnstwist`, we ran:<br/>
# MAGIC ```
# MAGIC dnstwist --registered google.com >> domains_dnstwists.csv
# MAGIC addition       googlea.com    184.168.131.241 NS:ns65.domaincontrol.com MX:mailstore1.secureserver.net
# MAGIC addition       googleb.com    47.254.33.193 NS:ns3.dns.com 
# MAGIC ```
# MAGIC 
# MAGIC We formatted domains_dnstwists.csv with a header: `PERMUTATIONTYPE,domain,meta`
# MAGIC 
# MAGIC Once you have created `domain_dnstwists.csv`, you can continue:
# MAGIC - load the dnstwisted domains
# MAGIC - enrich the table with domain names (without TLDs)
# MAGIC - load the `dnstwist`-enriched results into a silver table
# MAGIC 
# MAGIC We will use these tables later to productionize typosquatting detection.

# COMMAND ----------

# NOTE: domain_dnstwists.csv needs to be created outside of this notebook, using instructions from dnstwist. 
# Load the domain_dnstwists.csv into a dataframe, brand_domains_monitored_raw. Note the csv and header, true opetions.
brand_domains_monitored_raw_df = spark.read.csv(f"{get_default_path()}/datasets/domains_dnstwists.csv", header=True) 

# COMMAND ----------

# Display csv we just read
display(brand_domains_monitored_raw_df)

# COMMAND ----------

# Load the csv brand_domains_monitored_raw into a local table called, brand_domains_monitored_raw
brand_domains_monitored_raw_df.createOrReplaceTempView("brand_domains_monitored_raw")

# COMMAND ----------

# Extract the domain names using the UDF we created at Cmd 9 of this notebook.
# Create a new table with the dnstwist extracted domains. New column dnstwisted_domain
# The hardcoded ">=2" is there to accomodate for potential empty domain fileds
brand_domains_monitored_enriched_df = spark.sql("""
  select *, domain_extract(domain) as dnstwisted_domain
  from brand_domains_monitored_raw
  """).filter("char_length(dnstwisted_domain) >= 2")
display(brand_domains_monitored_enriched_df)

# COMMAND ----------

# Define a silver Delta table
(brand_domains_monitored_enriched_df.write
  .format("delta")
  .mode('overwrite')
  .option("mergeSchema", False)
  .saveAsTable("silver_twisted_domain_brand")
)

# COMMAND ----------

# MAGIC %sql
# MAGIC /* Query the silver Delta table */
# MAGIC select *  from silver_twisted_domain_brand
