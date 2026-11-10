import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import streamlit as st


def slug(value):
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def build_utm_url(destination, platform, campaign, content):
    parts = urlsplit(destination.strip())
    if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("Enter a complete HTTP or HTTPS destination URL.")
    if any(char.isspace() or ord(char) < 32 for char in destination.strip()):
        raise ValueError("The destination URL contains spaces or control characters.")
    source, medium = {"Facebook": ("facebook", "organic_social"), "Shopify Blog": ("shopify_blog", "content")}[platform]
    campaign, content = slug(campaign), slug(content)
    if not campaign or not content:
        raise ValueError("Enter a campaign and a unique content identifier.")
    tags = {"utm_source": source, "utm_medium": medium, "utm_campaign": campaign, "utm_content": content}
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key not in tags]
    return urlunsplit(parts._replace(query=urlencode([*query, *tags.items()])))


def show_utm_builder(base, source, items):
    st.caption("Use tagged links for visits from external placements. Tags can identify a clicked link when tracking "
               "records the visit. They cannot establish who only viewed a post or whether the post caused a purchase. "
               "Use the canonical URL for internal Shopify navigation and check whether your placement supports clickable links.")
    content_items = {item["id"]: item for item in items if item["source"] == source}
    key = f"utm:{base}:{source}"
    item_id = st.selectbox("Related content", [None, *content_items], key=f"{key}:item",
                           format_func=lambda value: content_items[value]["title"] if value else "New content")
    with st.form(f"{key}:form"):
        destination = st.text_input("Destination URL")
        platform = st.selectbox("Marketing platform", ("Facebook", "Shopify Blog"),
                                index=0 if source == "facebook" else 1)
        campaign = st.text_input("Campaign name")
        kind = st.selectbox("Content type", ("Post", "Reel", "Photo", "Link post", "Article"))
        content = st.text_input("Unique content identifier")
        if st.form_submit_button("Generate link"):
            st.session_state.pop(key, None)
            try:
                url = build_utm_url(destination, platform, campaign, f"{kind}_{content}" if content.strip() else "")
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.session_state[key] = item_id, url
    saved = st.session_state.get(key)
    if saved and saved[0] == item_id:
        st.caption("Link draft for this session. The code block includes a copy button.")
        st.code(saved[1], language=None)
