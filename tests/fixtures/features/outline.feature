Feature: Balances

  Scenario Outline: Pay an amount
    When the user pays <amount>
    Then the balance should be "<amount>"

    Examples:
      | amount |
      | 10.00  |
      | 20.50  |
