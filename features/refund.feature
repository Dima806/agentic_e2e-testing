Feature: Refund a payment
  Runs against the demo site in examples/demo-site (make demo-site).

  Scenario: A support agent refunds a successful payment
    Given a user is on the dashboard page
    When the user opens the Payments section
    And the user hovers over payment P-1001, finds the refund link, clicks it to open the refund page
    Then the refund page for payment "P-1001" should be visible
    When the user submits the refund request
    Then the refund status page should be visible
    Then the refunds table should be shown with following values
      """
      | Status     | Payment | Reference | Amount |
      | Successful | P-1001  | <ref>     | 272.00 |
      """
