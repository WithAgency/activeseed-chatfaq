import os
from logging import getLogger
from urllib.parse import urljoin

import ray

from back.apps.language_model.models.enums import (
    DeviceChoices,
    RetrieverTypeChoices,
)

logger = getLogger(__name__)


def generate_titles(contents, n_titles, lang):
    from chat_rag.inf_retrieval.query_generator import QueryGenerator
    from tqdm import tqdm

    api_key = os.environ.get("OPENAI_API_KEY", None)
    query_generator = QueryGenerator(api_key, lang=lang)

    new_titles = []
    for content in tqdm(contents):
        titles = query_generator(content, n_queries=n_titles)
        new_titles.append(titles)

    return new_titles


@ray.remote(num_cpus=1, resources={"tasks": 1})
def generate_titles_task(knowledge_base_pk, n_titles=10):
    """
    Generate titles for the knowledge items of a knowledge base.
    Parameters
    ----------
    knowledge_base_pk : int
        The primary key of the knowledge base.
    n_titles : int
        The number of titles to generate for each knowledge item.
    """

    from back.apps.language_model.models import KnowledgeBase, KnowledgeItem, AutoGeneratedTitle

    kb = KnowledgeBase.objects.get(pk=knowledge_base_pk)
    k_items = KnowledgeItem.objects.filter(knowledge_base=knowledge_base_pk)

    contents = [item.content for item in k_items]
    titles = generate_titles(
        contents, n_titles, kb.get_lang().value
    )

    for item_titles, item in zip(titles, k_items):
        new_titles = [
            AutoGeneratedTitle(
                knowledge_item=item,
                title=title,
            )
            for title in item_titles
        ]
        AutoGeneratedTitle.objects.bulk_create(new_titles)

    print(f"Titles generated for knowledge base: {kb.name}")


@ray.remote(num_cpus=1, resources={"tasks": 1})
def generate_intents(clusters):
    from chat_rag.intent_detection import generate_intents

    print("Generating intents...")
    intents = generate_intents(clusters)
    return intents


@ray.remote(num_cpus=1, resources={"tasks": 1}, num_returns=2)
def get_similarity_scores(titles, rag_config_id, e5_model_args, batch_size):
    def retrieve(queries, rag_config_id, e5_model_args, batch_size, top_k=1):
        import requests
        import os
        from chat_rag.inf_retrieval.embedding_models import E5Model

        e5_model = E5Model(**e5_model_args, huggingface_key=os.environ.get("HUGGINGFACE_API_KEY", None))

        embeddings = e5_model.build_embeddings(queries, prefix='query: ', batch_size=batch_size)

        token = os.getenv('BACKEND_TOKEN')
        retrieve_endpoint = urljoin(os.environ.get('BACKEND_HOST'), f"/back/api/language-model/rag-configs/{rag_config_id}/retrieve/")

        headers = {'Authorization': f'Token {token}'}

        response = requests.post(retrieve_endpoint, json={'query_embeddings': embeddings.tolist(), 'top_k': 1}, headers=headers)

        return response.json()

    import numpy as np

    results = retrieve(titles, rag_config_id, e5_model_args, batch_size)
    similarities = [item["similarity"] for item in results]
    mean_similarity = np.mean(similarities)
    std_similarity = np.std(similarities)

    return mean_similarity, std_similarity


@ray.remote(num_cpus=1, resources={"tasks": 1})
def clusterize_queries(queries, e5_model_args, batch_size):

    from chat_rag.inf_retrieval.embedding_models import E5Model
    from chat_rag.intent_detection import clusterize_text

    e5_model = E5Model(**e5_model_args, huggingface_key=os.environ.get("HUGGINGFACE_API_KEY", None))

    print("Clusterizing queries...")
    labels = clusterize_text(
        queries,
        e5_model,
        batch_size=batch_size,
        prefix="query: ",
    )
    print("Done!")

    return labels


@ray.remote(num_cpus=0.5, resources={"tasks": 1})
def generate_suggested_intents_task(knowledge_base_pk, _generate_titles=False):
    """
    Generate new intents from the users' queries. Orchestrator task that calls the other tasks.
    Parameters
    ----------
    knowledge_base_pk : int
        The primary key of the knowledge base.
    """
    if _generate_titles:
        task_name = f"generate_titles_{knowledge_base_pk}"
        # block until the task is finished
        ray.get(generate_titles_task.options(name=task_name).remote(knowledge_base_pk))

    from django.db.models import Max

    from back.apps.language_model.prompt_templates import get_queries_out_of_domain
    from back.apps.language_model.models import RAGConfig, MessageKnowledgeItem, AutoGeneratedTitle, Intent
    from back.apps.broker.models.message import Message

    print("generate_new_intents_task called")

    # These are in domain titles
    titles_in_domain = AutoGeneratedTitle.objects.filter(
        knowledge_item__knowledge_base=knowledge_base_pk
    )[:100]

    # Get the RAG config that corresponds to the knowledge base
    rag_conf = RAGConfig.objects.filter(knowledge_base=knowledge_base_pk).first()
    if not rag_conf:
        print(f"No RAG config found for knowledge base: {knowledge_base_pk}")
        return
    lang = rag_conf.knowledge_base.get_lang().value

    # if the retriever type is not e5, then return
    if rag_conf.retriever_config.get_retriever_type() != RetrieverTypeChoices.E5:
        print(f"Intent generation is not supported for retriever type: {rag_conf.retriever_config.get_retriever_type().value} right now")
        return

    e5_model_args = {
        "model_name": rag_conf.retriever_config.model_name,
        "use_cpu": rag_conf.retriever_config.get_device() == DeviceChoices.CPU,
    }

    task_name = f"get_similarity_scores_{knowledge_base_pk}_in_domain"
    titles_in_domain_str = [title.title for title in titles_in_domain]
    in_domain_task_ref = get_similarity_scores.options(name=task_name).remote(titles_in_domain_str, rag_conf.pk, e5_model_args, rag_conf.retriever_config.batch_size)

    task_name = f"get_similarity_scores_{knowledge_base_pk}_out_domain"
    title_out_domain = get_queries_out_of_domain(lang)
    out_domain_task_ref = get_similarity_scores.options(name=task_name).remote(title_out_domain, rag_conf.pk, e5_model_args, rag_conf.retriever_config.batch_size)

    mean_sim_in_domain, std_sim_in_domain = ray.get(in_domain_task_ref)
    mean_sim_out_domain, std_sim_out_domain = ray.get(out_domain_task_ref)

    print(
        f"Mean similarity in domain: {mean_sim_in_domain}, std: {std_sim_in_domain}"
    )
    print(
        f"Mean similarity out domain: {mean_sim_out_domain}, std: {std_sim_out_domain}"
    )

    # The suggested new intents will have a similarity score between the in domain queries and the out of domain queries
    new_intents_thresholds = {
        "max": mean_sim_in_domain - std_sim_in_domain,
        "min": mean_sim_out_domain + std_sim_out_domain,
    }

    print(f"Suggested intents thresholds: {new_intents_thresholds}")

    # check that the max is greater than the min
    if new_intents_thresholds["max"] < new_intents_thresholds["min"]:
        print(
            "Max threshold is lower than min threshold, no new intents will be generated"
        )
        return

    messages = MessageKnowledgeItem.objects.filter(
        knowledge_item__knowledge_base_id=knowledge_base_pk  # Filter by knowledge base
    ).values("message_id").annotate(
        max_similarity=Max("similarity")
    ) #

    print(f"Number of messages: {messages.count()}")

    # filter the results if the max similarity is between the thresholds
    messages = messages.filter(
        max_similarity__lte=new_intents_thresholds["max"],
        max_similarity__gte=new_intents_thresholds["min"],
    )

    print(f"Number of messages after filtering: {messages.count()}")

    if messages.count() == 0:
        print("There are no suggested intents to generate")
        return

    messages_text = [
        Message.objects.get(id=item["message_id"]).stack[0]["payload"]
        for item in messages
    ]

    task_name = f"clusterize_queries_{knowledge_base_pk}"
    labels = ray.get(clusterize_queries.options(name=task_name).remote(messages_text, e5_model_args, rag_conf.retriever_config.batch_size))

    k_clusters = len(set(labels)) - (1 if -1 in labels else 0)
    print(f"Number of clusters: {k_clusters}")

    # list of lists of queries associated to each cluster
    clusters = [[] for _ in range(k_clusters)]
    cluster_instances = [[] for _ in range(k_clusters)]
    for label, query, message_instace in zip(labels, messages_text, messages):
        if label != -1:  # -1 is the label for outliers
            clusters[label].append(query)
            cluster_instances[label].append(message_instace)

    # generate the intents
    task_name = f"generate_intents_{knowledge_base_pk}"
    intents = ray.get(generate_intents.options(name=task_name).remote(clusters))

    # save the intents
    new_intents = [
        Intent(
            intent_name=intent,
            auto_generated=True,
            valid=False,
            suggested_intent=True,
        )
        for intent in intents
    ]

    Intent.objects.bulk_create(new_intents)

    print(f"Number of new intents: {len(new_intents)}")

    # add the messages to each intent
    for intent_cluster, intent in zip(cluster_instances, new_intents):
        # get the value of key 'message_id' from each message
        intent_cluster = [item["message_id"] for item in intent_cluster]
        intent.message.add(*intent_cluster)

    print("New intents generated successfully")


@ray.remote(num_cpus=0.5, resources={"tasks": 1})
def generate_intents_task(knowledge_base_pk, _generate_titles=False):
    """
    Generate existing intents from a knowledge base. Orchestrator task that calls the other tasks.
    Parameters
    ----------
    knowledge_base_pk : int
        The primary key of the knowledge base.
    """
    if _generate_titles:
        task_name = f"generate_titles_{knowledge_base_pk}"
        # block until the task is finished
        ray.get(generate_titles_task.options(name=task_name).remote(knowledge_base_pk))

    from back.apps.language_model.models import AutoGeneratedTitle, Intent, RAGConfig, KnowledgeItem

    rag_conf = RAGConfig.objects.filter(knowledge_base=knowledge_base_pk).first()
    if not rag_conf:
        print(f"No RAG config found for knowledge base: {knowledge_base_pk}")
        return

    # if the retriever type is not e5, then return
    if rag_conf.retriever_config.get_retriever_type() != RetrieverTypeChoices.E5:
        print(f"Intent generation is not supported for retriever type: {rag_conf.retriever_config.get_retriever_type().value} right now")
        return


    k_items = KnowledgeItem.objects.filter(knowledge_base=knowledge_base_pk)

    print(f"Generating intents for {k_items.count()} knowledge items")

    # These are in domain titles
    autogen_titles = AutoGeneratedTitle.objects.filter(
        knowledge_item__knowledge_base=knowledge_base_pk
    )

    # get as maximum 10 autogen_titles per knowledge item
    final_autogen_titles = []
    for item in k_items:
        titles = autogen_titles.filter(knowledge_item=item)[:10]
        final_autogen_titles.extend(titles)

    print(f"Number of titles: {len(final_autogen_titles)}")

    # get the queries
    queries = [title.title for title in final_autogen_titles]

    e5_model_args = {
        "model_name": rag_conf.retriever_config.model_name,
        "use_cpu": rag_conf.retriever_config.get_device() == DeviceChoices.CPU,
    }


    # clusterize the queries
    print("Clusterizing queries...")
    task_name = f"clusterize_queries_{knowledge_base_pk}"
    labels = ray.get(clusterize_queries.options(name=task_name).remote(queries, e5_model_args, rag_conf.retriever_config.batch_size))
    k_clusters = len(set(labels)) - (1 if -1 in labels else 0)
    print(f"Number of clusters: {k_clusters}")

    # list of lists of queries associated to each cluster
    clusters = [[] for _ in range(k_clusters)]
    cluster_instances = [[] for _ in range(k_clusters)]
    for label, query, title_instance in zip(labels, queries, final_autogen_titles):
        if label != -1:  # -1 is the label for outliers
            clusters[label].append(query)
            cluster_instances[label].append(title_instance)

    # generate the intents
    print("Generating intents...")
    task_name = f"generate_intents_{knowledge_base_pk}"
    intents = ray.get(generate_intents.options(name=task_name).remote(clusters))

    print(f"Number of new intents: {len(intents)} generated")

    # save the intents
    new_intents = [
        Intent(
            intent_name=intent,
            auto_generated=True,
            valid=False,
            suggested_intent=False,
        )
        for intent in intents
    ]

    Intent.objects.bulk_create(new_intents)

    print("Suggested intents saved successfully")

    # add the knowledge items to each intent
    for intent_cluster, intent in zip(cluster_instances, new_intents):
        # get the knowledge items from each title
        intent_cluster = [item.knowledge_item for item in intent_cluster]
        # remove duplicated knowledge items
        intent_cluster = list(set(intent_cluster))
        intent.knowledge_item.add(*intent_cluster)

    print("Knowledge items added to the intents successfully")