import unittest

from scripts.feishu_delivery import resolve_delivery_target


class FeishuDeliveryTargetTests(unittest.TestCase):
    def test_prefers_base_chat_over_user_and_topic_entries(self):
        directory = {
            "platforms": {
                "feishu": [
                    {"id": "ou_user", "type": "dm"},
                    {
                        "id": "oc_chat:om_topic",
                        "type": "dm",
                        "thread_id": "om_topic",
                    },
                    {"id": "oc_chat", "type": "dm", "thread_id": None},
                ]
            }
        }

        self.assertEqual(resolve_delivery_target(directory), ("chat", "oc_chat"))

    def test_strips_topic_suffix_when_only_thread_entry_is_available(self):
        directory = {
            "platforms": {
                "feishu": [
                    {"id": "oc_chat:om_topic", "thread_id": "om_topic"},
                ]
            }
        }

        self.assertEqual(resolve_delivery_target(directory), ("chat", "oc_chat"))

    def test_falls_back_to_user_when_no_chat_is_available(self):
        directory = {"platforms": {"feishu": [{"id": "ou_user"}]}}

        self.assertEqual(resolve_delivery_target(directory), ("user", "ou_user"))


if __name__ == "__main__":
    unittest.main()
