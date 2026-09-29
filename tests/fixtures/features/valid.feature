Feature: Payments

  Background:
    Given a user is on the dashboard page

  Scenario: Refund a payment
    When the user opens the Payments section
    And the user hovers over payment P-1001
    But the user does not refund payment P-1000
    Then the refund page should be visible
    And the total should be 272.00 for "Alice"
    Then the refunds table should be shown with following values
      """
      | Status     | Payment | Amount |
      | Successful | P-1001  | 272.00 |
      | a\|b       | c\\d    | <ref>  |
      """
    And the summary table shows
      | Label | Amount   |
      | x\\y  | 1,234.50 |
    And the confirmation reads
      """
      Refund requested
      """

  Scenario: Fill a form with input data
    When the user fills the form with
      | Field  | Value |
      | Amount | 10.00 |
    Then the form should be saved
